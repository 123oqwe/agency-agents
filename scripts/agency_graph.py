#!/usr/bin/env python3
"""
agency_graph.py — compose Agency agents into automated teams: graphs with loops.

A *team graph* (strategy/graphs/<slug>.json) is a declarative program whose
instructions are agents. It has five node types:

  agent  one specialist call (any agent .md in this repo, referenced by slug)
  gate   an agent call that must end with `VERDICT: <X>`; the verdict drives
         `when` branches and loop exit conditions
  graph  call another team graph as a sub-team (composition)
  loop   repeat a body until a condition holds, at most `max_iterations` times,
         feeding each iteration the previous one's feedback (Dev<->QA loops)
  map    run a body once per item of a list (fan-out, e.g. one Dev<->QA loop
         per task a PM produced), optionally in parallel

Nodes are wired by `after` (data dependencies, must form a DAG). Independent
nodes run concurrently. Cycles exist only as `loop` nodes, which are bounded,
and graph references may not recurse — so every graph has a finite, statically
computable worst-case number of agent calls (`validate` and `show` print it).

Runs are checkpointed after every agent call, so an interrupted run (Ctrl-C,
--max-calls budget, a crashed backend) resumes where it stopped with --resume.

No dependencies beyond the Python 3.8+ standard library, except the optional
`anthropic` package for the `anthropic` backend.

Usage:
  scripts/agency_graph.py list
  scripts/agency_graph.py validate [graph ...]
  scripts/agency_graph.py show <graph>
  scripts/agency_graph.py mermaid <graph> [--expand]
  scripts/agency_graph.py roster <graph> [--csv]
  scripts/agency_graph.py run <graph> [--input k=v ...] [--backend mock|claude-code|anthropic]
  scripts/agency_graph.py run --resume <run-dir>

See strategy/graphs/README.md for the format reference.
"""

import argparse
import concurrent.futures as cf
import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GRAPH_DIR = os.path.join(ROOT, "strategy", "graphs")
NON_AGENT_DIRS = {"integrations", "examples", "strategy", "scripts"}

NODE_TYPES = ("agent", "gate", "graph", "loop", "map")
DEFAULT_VERDICTS = ["PASS", "FAIL", "BLOCKED"]
STATUSES = ("done", "skipped", "passed", "broken", "exhausted")
ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
TEMPLATE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")
VERDICT_RE = re.compile(r"^\W*VERDICT\W*:\W*([A-Za-z_]+)", re.IGNORECASE | re.MULTILINE)
JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
# A map's verdict is PASS when every item's verdict is one of these (or absent).
DEFAULT_PASS_VERDICTS = ["PASS", "APPROVED", "READY"]
MAX_LOOP_ITERATIONS = 10
DEFAULT_MAX_ITEMS = 20
CONTEXT_CHAR_LIMIT = 60000


class GraphError(Exception):
    """A graph spec is invalid, or a run cannot continue."""


class BudgetExceeded(Exception):
    """--max-calls was reached; the run is checkpointed and can be resumed."""


# ---------------------------------------------------------------------------
# Agent catalog
# ---------------------------------------------------------------------------

class Catalog:
    """Agent slug -> file path, display name, emoji, and prompt body."""

    def __init__(self, root=ROOT):
        self.root = root
        self.agents = {}
        for dirpath, dirnames, filenames in os.walk(root):
            rel = os.path.relpath(dirpath, root)
            top = rel.split(os.sep)[0]
            if rel == ".":
                dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in NON_AGENT_DIRS]
                continue
            for name in filenames:
                if not name.endswith(".md"):
                    continue
                path = os.path.join(dirpath, name)
                meta, _ = _split_frontmatter(path)
                if meta is None or "name" not in meta:
                    continue
                self.agents[name[:-3]] = {"path": path, "division": top, **meta}

    def __contains__(self, slug):
        return slug in self.agents

    def label(self, slug):
        a = self.agents.get(slug)
        if not a:
            return slug
        return (a.get("emoji", "") + " " + a["name"]).strip()

    def prompt(self, slug):
        _, body = _split_frontmatter(self.agents[slug]["path"])
        return body.strip()


def _split_frontmatter(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---", 4)
    if end < 0:
        return None, text
    meta = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, text[end + 4:]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

class Library:
    """All graphs under strategy/graphs/, plus any explicitly loaded files."""

    def __init__(self, graph_dir=GRAPH_DIR):
        self.graphs = {}
        self.paths = {}
        if os.path.isdir(graph_dir):
            for name in sorted(os.listdir(graph_dir)):
                if name.endswith(".json"):
                    self.load(os.path.join(graph_dir, name))

    def load(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                g = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            raise GraphError(f"{path}: cannot load graph: {e}")
        slug = g.get("slug") or os.path.basename(path)[:-5]
        self.graphs[slug] = g
        self.paths[slug] = path
        return slug

    def resolve(self, ref):
        """Accept a slug or a path to a .json file."""
        if ref in self.graphs:
            return ref
        if ref.endswith(".json") and os.path.isfile(ref):
            return self.load(ref)
        raise GraphError(f"unknown graph '{ref}' (see `agency_graph.py list`)")

    def get(self, slug):
        if slug not in self.graphs:
            raise GraphError(f"unknown graph '{slug}'")
        return self.graphs[slug]


# ---------------------------------------------------------------------------
# Conditions (`when`, loop `until` / `break`)
# ---------------------------------------------------------------------------

def cond_nodes(cond):
    """Node ids a condition refers to."""
    if isinstance(cond, list):
        return [n for c in cond for n in cond_nodes(c)]
    if "all" in cond:
        return cond_nodes(cond["all"])
    if "any" in cond:
        return cond_nodes(cond["any"])
    return [cond.get("node")]


def eval_cond(cond, results):
    if isinstance(cond, list):
        return all(eval_cond(c, results) for c in cond)
    if "all" in cond:
        return all(eval_cond(c, results) for c in cond["all"])
    if "any" in cond:
        return any(eval_cond(c, results) for c in cond["any"])
    r = results.get(cond["node"])
    if r is None:
        ok = False
    elif "verdict" in cond:
        ok = r.get("verdict") in _as_list(cond["verdict"])
    else:
        ok = r.get("status") in _as_list(cond["status"])
    return not ok if cond.get("negate") else ok


def _as_list(v):
    return v if isinstance(v, list) else [v]


def describe_cond(cond):
    if isinstance(cond, list):
        return " and ".join(describe_cond(c) for c in cond)
    if "all" in cond:
        return "(" + " and ".join(describe_cond(c) for c in cond["all"]) + ")"
    if "any" in cond:
        return "(" + " or ".join(describe_cond(c) for c in cond["any"]) + ")"
    key = "verdict" if "verdict" in cond else "status"
    op = "!=" if cond.get("negate") else "="
    return f"{cond['node']}.{key}{op}{'/'.join(_as_list(cond[key]))}"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class Validator:
    """Static checks. Every problem is collected, not just the first."""

    def __init__(self, library, catalog):
        self.lib = library
        self.cat = catalog
        self.errors = []
        self._bounds = {}

    def err(self, where, msg):
        self.errors.append(f"{where}: {msg}")

    def validate_all(self, slugs=None):
        for slug in slugs or sorted(self.lib.graphs):
            self.validate_graph(slug)
        self.check_recursion()
        return self.errors

    # -- graph level --------------------------------------------------------

    def validate_graph(self, slug):
        g = self.lib.get(slug)
        where = os.path.relpath(self.lib.paths.get(slug, slug), ROOT)
        fname = os.path.basename(self.lib.paths.get(slug, slug + ".json"))[:-5]
        if g.get("slug") != fname:
            self.err(where, f"\"slug\" must equal the file name stem '{fname}'")
        for field in ("title", "summary"):
            if not isinstance(g.get(field), str) or not g.get(field):
                self.err(where, f"missing \"{field}\"")
        if g.get("kind") not in ("team", "block"):
            self.err(where, "\"kind\" must be \"team\" (runnable deliverable) or \"block\" (reusable sub-team)")
        inputs = g.get("inputs", {})
        if not isinstance(inputs, dict):
            self.err(where, "\"inputs\" must be an object")
            inputs = {}
        for name, spec in inputs.items():
            if not ID_RE.match(name) or not isinstance(spec, dict):
                self.err(where, f"input '{name}' must be a lowercase id mapped to an object")
        pool = g.get("pool", [])
        if not isinstance(pool, list):
            self.err(where, "\"pool\" must be a list of agent slugs")
            pool = []
        for a in pool:
            if a not in self.cat:
                self.err(where, f"pool agent '{a}' does not match any agent .md filename stem")
        scope = {"inputs": set(inputs), "input_defaults": inputs, "outer": set(),
                 "item": False, "loop": False}
        self.validate_body(g, where, scope)

    def validate_body(self, body, where, scope):
        """Validate a graph or an inline loop/map body. Returns {id: node}."""
        nodes = body.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            self.err(where, "\"nodes\" must be a non-empty list")
            return {}
        by_id = {}
        for i, n in enumerate(nodes):
            nid = n.get("id") if isinstance(n, dict) else None
            if not isinstance(nid, str) or not ID_RE.match(nid):
                self.err(where, f"node #{i + 1} needs an \"id\" matching {ID_RE.pattern}")
                continue
            if nid in by_id:
                self.err(where, f"duplicate node id '{nid}'")
            by_id[nid] = n
        for nid, n in by_id.items():
            for dep in n.get("after", []):
                if dep not in by_id:
                    self.err(f"{where} › {nid}", f"\"after\" references unknown node '{dep}'")
        order = topo_order(by_id)
        if order is None:
            self.err(where, "\"after\" dependencies contain a cycle — use a `loop` node for iteration")
            return by_id
        ancestors = ancestor_sets(by_id, order)
        for nid in order:
            self.validate_node(by_id[nid], f"{where} › {nid}", scope, ancestors[nid], by_id)
        for key, tmpl in (body.get("outputs") or {}).items():
            self.check_template(tmpl, f"{where} › outputs.{key}", scope, set(by_id), by_id)
        return by_id

    # -- node level ---------------------------------------------------------

    def validate_node(self, n, where, scope, ancestors, siblings):
        t = n.get("type")
        if t not in NODE_TYPES:
            self.err(where, f"\"type\" must be one of {', '.join(NODE_TYPES)}")
            return
        if "when" in n:
            for ref in cond_nodes(n["when"]):
                if ref not in ancestors:
                    self.err(where, f"\"when\" references '{ref}', which must be an upstream node (reachable via \"after\")")
            self.check_cond_values(n["when"], where, siblings)
        for ref in n.get("context", []):
            if ref not in ancestors:
                self.err(where, f"\"context\" references '{ref}', which must be an upstream node")
        if t in ("agent", "gate"):
            self.check_agent_ref(n.get("agent"), where, scope)
            if not isinstance(n.get("task"), str) or not n["task"].strip():
                self.err(where, "needs a non-empty \"task\"")
            self.check_template(n.get("task", ""), where, scope, ancestors, siblings)
            if t == "gate":
                v = n.get("verdicts", DEFAULT_VERDICTS)
                if not isinstance(v, list) or not v or not all(isinstance(x, str) and re.match(r"^[A-Z_]+$", x) for x in v):
                    self.err(where, "\"verdicts\" must be a list of UPPER_CASE words")
                if n.get("output") == "json":
                    self.err(where, "a gate cannot also have \"output\": \"json\"")
            if n.get("output") not in (None, "text", "json"):
                self.err(where, "\"output\" must be \"text\" or \"json\"")
        elif t == "graph":
            self.check_graph_call(n.get("graph"), n.get("inputs", {}), where, scope, ancestors, siblings)
        elif t == "loop":
            mi = n.get("max_iterations")
            if not isinstance(mi, int) or not 1 <= mi <= MAX_LOOP_ITERATIONS:
                self.err(where, f"\"max_iterations\" must be an integer 1..{MAX_LOOP_ITERATIONS} (loops are always bounded)")
            if "until" not in n:
                self.err(where, "a loop needs an \"until\" exit condition")
            body_ids = self.validate_composite_body(n, where, scope, ancestors, loop=True)
            if "feedback" in n:
                self.check_template(n["feedback"], f"{where} › feedback", scope, set(body_ids), body_ids)
            for key in ("until", "break"):
                if key in n:
                    for ref in cond_nodes(n[key]):
                        if ref not in body_ids:
                            self.err(where, f"\"{key}\" references '{ref}', which is not a node of the loop body")
                    self.check_cond_values(n[key], where, body_ids)
        elif t == "map":
            items = n.get("items")
            if isinstance(items, str):
                head = items.split(".")
                if head[0] == "input":
                    if len(head) < 2 or head[1] not in scope["inputs"]:
                        self.err(where, f"\"items\" references undeclared input '{items}'")
                elif head[0] == "nodes":
                    if len(head) < 3 or head[1] not in ancestors:
                        self.err(where, f"\"items\" path '{items}' must start with an upstream node: nodes.<id>.<field>")
                else:
                    self.err(where, "\"items\" must be a path (nodes.<id>.json / input.<name>) or a literal list")
            elif not isinstance(items, list):
                self.err(where, "\"items\" must be a path or a literal list")
            for key in ("max_items", "max_parallel"):
                v = n.get(key)
                if v is not None and (not isinstance(v, int) or v < 1):
                    self.err(where, f"\"{key}\" must be a positive integer")
            self.validate_composite_body(n, where, scope, ancestors, item=True)

    def validate_composite_body(self, n, where, scope, ancestors, loop=False, item=False):
        body = n.get("body")
        if not isinstance(body, dict):
            self.err(where, "needs a \"body\": {\"nodes\": [...]} or {\"graph\": \"<slug>\", \"inputs\": {...}}")
            return set()
        inner = dict(scope, outer=scope["outer"] | set(ancestors),
                     loop=scope["loop"] or loop, item=scope["item"] or item)
        if "graph" in body:
            self.check_graph_call(body["graph"], body.get("inputs", {}), where, inner, set(ancestors), {})
            # A graph body is addressed as the single node "body" by until/break.
            return {"body": {"type": "graph"}}
        return self.validate_body(body, where + " › body", inner)

    def check_graph_call(self, slug, inputs, where, scope, ancestors, siblings):
        if slug not in self.lib.graphs:
            self.err(where, f"references unknown graph '{slug}'")
            return
        declared = self.lib.get(slug).get("inputs", {})
        if not isinstance(inputs, dict):
            self.err(where, "\"inputs\" must be an object")
            return
        for k in inputs:
            if k not in declared:
                self.err(where, f"graph '{slug}' has no input '{k}'")
        for k, spec in declared.items():
            if isinstance(spec, dict) and spec.get("required") and k not in inputs:
                self.err(where, f"graph '{slug}' requires input '{k}'")
        for k, v in inputs.items():
            if isinstance(v, str):
                self.check_template(v, f"{where} › inputs.{k}", scope, ancestors, siblings)
                if k in declared and declared[k].get("type") == "agent":
                    self.check_agent_ref(v, f"{where} › inputs.{k}", scope)

    def check_agent_ref(self, ref, where, scope):
        if not isinstance(ref, str) or not ref:
            self.err(where, "needs an \"agent\" slug")
            return
        m = TEMPLATE_RE.fullmatch(ref.strip())
        if not m:
            if ref not in self.cat:
                self.err(where, f"agent '{ref}' does not match any agent .md filename stem")
            return
        path = m.group(1).split(".")
        if path[0] == "input":
            name = path[1] if len(path) > 1 else ""
            if name not in scope["inputs"]:
                self.err(where, f"agent template references undeclared input '{name}'")
                return
            spec = scope["input_defaults"].get(name, {})
            if spec.get("type") != "agent":
                self.err(where, f"input '{name}' is used as an agent — declare it with \"type\": \"agent\"")
            d = spec.get("default")
            if d is not None and d not in self.cat:
                self.err(where, f"default agent '{d}' for input '{name}' does not exist")
        elif path[0] == "item":
            if not scope["item"]:
                self.err(where, "{{item...}} is only available inside a map body")
        else:
            self.err(where, f"agent must be a slug, {{{{input.<name>}}}} or {{{{item.<field>}}}}, not '{ref}'")

    def check_template(self, text, where, scope, ancestors, siblings):
        if not isinstance(text, str):
            return
        for m in TEMPLATE_RE.finditer(text):
            path = m.group(1).split(".")
            head = path[0]
            if head == "input":
                if len(path) < 2 or path[1] not in scope["inputs"]:
                    self.err(where, f"{{{{{m.group(1)}}}}}: undeclared input")
            elif head == "nodes":
                if len(path) < 2:
                    self.err(where, f"{{{{{m.group(1)}}}}}: expected nodes.<id>.<field>")
                elif path[1] not in ancestors and path[1] not in scope["outer"]:
                    self.err(where, f"{{{{{m.group(1)}}}}}: '{path[1]}' is not upstream of this node (add it to \"after\")")
            elif head == "item":
                if not scope["item"]:
                    self.err(where, f"{{{{{m.group(1)}}}}}: only available inside a map body")
            elif head == "loop":
                if not scope["loop"]:
                    self.err(where, f"{{{{{m.group(1)}}}}}: only available inside a loop body")
            else:
                self.err(where, f"{{{{{m.group(1)}}}}}: unknown namespace '{head}' (use input, nodes, item, loop)")

    def check_cond_values(self, cond, where, nodes):
        for c in _flatten_conds(cond):
            if ("verdict" in c) == ("status" in c):
                self.err(where, f"condition on '{c.get('node')}' needs exactly one of \"verdict\" / \"status\"")
                continue
            if "status" in c:
                bad = [s for s in _as_list(c["status"]) if s not in STATUSES]
                if bad:
                    self.err(where, f"unknown status {bad} (valid: {', '.join(STATUSES)})")
            n = nodes.get(c.get("node")) if isinstance(nodes, dict) else None
            if "verdict" in c and n and n.get("type") == "gate":
                allowed = n.get("verdicts", DEFAULT_VERDICTS)
                bad = [v for v in _as_list(c["verdict"]) if v not in allowed]
                if bad:
                    self.err(where, f"gate '{c['node']}' can never return {bad} (its verdicts: {allowed})")

    # -- composition --------------------------------------------------------

    def check_recursion(self):
        edges = {s: set(graph_refs(g)) for s, g in self.lib.graphs.items()}
        state = {}

        def visit(s, stack):
            state[s] = 1
            for t in edges.get(s, ()):
                if state.get(t) == 1:
                    self.err("composition", "graph references recurse: " + " → ".join(stack + [t]))
                elif t in edges and not state.get(t):
                    visit(t, stack + [t])
            state[s] = 2

        for s in edges:
            if not state.get(s):
                visit(s, [s])


def _flatten_conds(cond):
    if isinstance(cond, list):
        return [x for c in cond for x in _flatten_conds(c)]
    if "all" in cond:
        return _flatten_conds(cond["all"])
    if "any" in cond:
        return _flatten_conds(cond["any"])
    return [cond]


def topo_order(by_id):
    """Kahn's algorithm, stable w.r.t. declaration order. None on a cycle."""
    indeg = {k: len([d for d in v.get("after", []) if d in by_id]) for k, v in by_id.items()}
    order, ready = [], [k for k in by_id if indeg[k] == 0]
    while ready:
        k = ready.pop(0)
        order.append(k)
        for j, v in by_id.items():
            if k in v.get("after", []):
                indeg[j] -= 1
                if indeg[j] == 0:
                    ready.append(j)
    return order if len(order) == len(by_id) else None


def ancestor_sets(by_id, order):
    anc = {}
    for k in order:
        s = set()
        for d in by_id[k].get("after", []):
            if d in anc:
                s |= anc[d] | {d}
        anc[k] = s
    return anc


def graph_refs(body):
    """Graph slugs referenced anywhere in a graph/body."""
    out = []
    for n in body.get("nodes", []):
        if not isinstance(n, dict):
            continue
        if n.get("type") == "graph" and n.get("graph"):
            out.append(n["graph"])
        b = n.get("body")
        if isinstance(b, dict):
            if "graph" in b:
                out.append(b["graph"])
            else:
                out += graph_refs(b)
    return out


# ---------------------------------------------------------------------------
# Static analysis: worst-case calls and roster
# ---------------------------------------------------------------------------

def call_bound(lib, body, memo=None):
    """Worst-case number of backend calls to run `body` (a graph or inline body)."""
    memo = {} if memo is None else memo
    total = 0
    for n in body.get("nodes", []):
        total += node_bound(lib, n, memo)
    return total


def node_bound(lib, n, memo):
    t = n.get("type")
    if t in ("agent", "gate"):
        return 2 if n.get("output") == "json" else 1  # +1 for a JSON repair call
    if t == "graph":
        return graph_bound(lib, n["graph"], memo)
    if t in ("loop", "map"):
        b = n["body"]
        inner = graph_bound(lib, b["graph"], memo) if "graph" in b else call_bound(lib, b, memo)
        if t == "loop":
            return n["max_iterations"] * inner
        cap = n.get("max_items", DEFAULT_MAX_ITEMS)
        if isinstance(n.get("items"), list):
            cap = min(cap, len(n["items"]))
        return cap * inner
    return 0


def graph_bound(lib, slug, memo):
    if slug not in memo:
        memo[slug] = 0  # recursion guard; validation reports recursion separately
        memo[slug] = call_bound(lib, lib.get(slug), memo)
    return memo[slug]


def roster(lib, graph, seen=None):
    """Every agent slug a graph can call. Agent-typed inputs contribute their
    defaults and any literal slugs passed to them; {{item.*}} choices are only
    known at run time."""
    seen = set() if seen is None else seen
    out = []

    def add(ref, inputs):
        m = TEMPLATE_RE.fullmatch((ref or "").strip())
        if not m:
            out.append(ref)
            return
        p = m.group(1).split(".")
        if p[0] == "input" and len(p) > 1:
            d = (inputs.get(p[1]) or {}).get("default")
            if d:
                out.append(d)

    def call(slug, call_inputs, inputs):
        declared = lib.get(slug).get("inputs", {})
        for k, v in call_inputs.items():
            if declared.get(k, {}).get("type") == "agent" and isinstance(v, str):
                add(v, inputs)
        if slug not in seen:
            seen.add(slug)
            out.extend(roster(lib, lib.get(slug), seen))

    def walk(body, inputs):
        for n in body.get("nodes", []):
            t = n.get("type")
            if t in ("agent", "gate"):
                add(n.get("agent"), inputs)
            elif t == "graph":
                call(n["graph"], n.get("inputs", {}), inputs)
            elif t in ("loop", "map"):
                b = n["body"]
                if "graph" in b:
                    call(b["graph"], b.get("inputs", {}), inputs)
                else:
                    walk(b, inputs)

    walk(graph, graph.get("inputs", {}))
    out.extend(graph.get("pool", []))
    uniq = []
    for a in out:
        if a and a not in uniq:
            uniq.append(a)
    return uniq


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def lookup(ctx, dotted):
    cur = ctx
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
        if cur is None:
            return None
    return cur


def render(text, ctx):
    def sub(m):
        v = lookup(ctx, m.group(1))
        if v is None:
            return ""
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False, indent=2)
        return str(v)
    return TEMPLATE_RE.sub(sub, text)


def render_value(value, ctx):
    """Like render(), but a template that is exactly one reference keeps its type."""
    if not isinstance(value, str):
        return value
    m = TEMPLATE_RE.fullmatch(value.strip())
    if m:
        v = lookup(ctx, m.group(1))
        return v if v is not None else ""
    return render(value, ctx)


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

class MockBackend:
    """Deterministic offline backend for dry runs, CI and tests.

    Gate verdicts come from --mock-verdict <node-id>=V1,V2,... (consumed in
    order, per node id, last value repeats); otherwise the first allowed
    verdict. JSON nodes return the node's "mock" field if present.
    """

    name = "mock"

    def __init__(self, verdicts=None):
        self.verdicts = {k: list(v) for k, v in (verdicts or {}).items()}
        self.lock = threading.Lock()

    def call(self, req):
        node = req["node"]
        lines = [f"[mock:{req['agent']}] {req['path']}", "", req["task"][:300]]
        if node.get("type") == "gate":
            allowed = node.get("verdicts", DEFAULT_VERDICTS)
            with self.lock:
                seq = self.verdicts.get(node["id"])
                v = (seq.pop(0) if len(seq) > 1 else seq[0]) if seq else allowed[0]
            lines += ["", f"Mock assessment: {v.lower()}.", f"VERDICT: {v}"]
        if node.get("output") == "json":
            sample = node.get("mock", [{"id": "T1", "title": "Mock task one"},
                                       {"id": "T2", "title": "Mock task two"}])
            lines += ["", "```json", json.dumps(sample, indent=2), "```"]
        return "\n".join(lines)


class ClaudeCodeBackend:
    """Runs each agent as a headless Claude Code session (`claude -p`) in --workdir.

    This is the "real work" backend: developer agents edit files, QA agents run
    tests, all inside the project directory. Extra CLI flags (e.g. a permission
    mode or allowed tools) come from --claude-args.
    """

    name = "claude-code"

    def __init__(self, workdir, extra_args="", model=None):
        self.workdir = workdir
        self.extra = shlex.split(extra_args or "")
        self.model = model

    def call(self, req):
        # The prompt goes over stdin: upstream context can exceed argv limits.
        cmd = ["claude", "-p", "--append-system-prompt", req["system"], "--output-format", "text"]
        model = req.get("model") or self.model
        if model:
            cmd += ["--model", model]
        cmd += self.extra
        try:
            p = subprocess.run(cmd, cwd=self.workdir, input=req["user"], capture_output=True, text=True)
        except FileNotFoundError:
            raise GraphError("`claude` CLI not found on PATH (install Claude Code, or use --backend anthropic/mock)")
        if p.returncode != 0:
            raise GraphError(f"claude exited {p.returncode}: {p.stderr.strip()[:2000]}")
        return p.stdout


class AnthropicBackend:
    """Calls the Claude API through the official `anthropic` Python SDK."""

    name = "anthropic"

    def __init__(self, model=None, effort=None, max_tokens=64000):
        try:
            import anthropic  # noqa: F401
        except ImportError:
            raise GraphError("the anthropic backend needs `pip install anthropic`")
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model or "claude-opus-5-5"
        self.effort = effort or "high"
        self.max_tokens = max_tokens

    def call(self, req):
        model = req.get("model") or self.model
        kwargs = dict(
            model=model,
            max_tokens=self.max_tokens,
            system=req["system"],
            messages=[{"role": "user", "content": req["user"]}],
            thinking={"type": "adaptive"},
            output_config={"effort": req.get("effort") or self.effort},
        )
        if model in ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"):
            # Server-side refusal fallback: a declined request is re-run on a
            # suitable model inside the same call instead of simply stopping.
            kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
        try:
            with self.client.beta.messages.stream(**kwargs) as stream:
                msg = stream.get_final_message()
        except TypeError:
            # SDK predates the typed `fallbacks` parameter: send it as a raw body field.
            kwargs["extra_body"] = {"fallbacks": kwargs.pop("fallbacks")}
            with self.client.beta.messages.stream(**kwargs) as stream:
                msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise GraphError(f"{req['path']}: the model declined this request")
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if msg.stop_reason == "max_tokens":
            text += "\n\n[agency-graph: output hit max_tokens and is incomplete]"
        return text


# ---------------------------------------------------------------------------
# Run state (checkpointing / resume)
# ---------------------------------------------------------------------------

class RunState:
    def __init__(self, run_dir, meta=None):
        self.dir = run_dir
        self.lock = threading.Lock()
        self.path = os.path.join(run_dir, "state.json")
        if os.path.isfile(self.path):
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.meta, self.calls = data["meta"], data["calls"]
        else:
            os.makedirs(os.path.join(run_dir, "nodes"), exist_ok=True)
            self.meta, self.calls = meta or {}, {}
            self.save()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"meta": self.meta, "calls": self.calls}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def get(self, path):
        with self.lock:
            return self.calls.get(path)

    def put(self, path, record, prompt=None):
        with self.lock:
            self.calls[path] = record
            base = os.path.join(self.dir, "nodes", re.sub(r"[^A-Za-z0-9_.-]+", "__", path))
            with open(base + ".md", "w", encoding="utf-8") as f:
                f.write(f"<!-- {path} · {record['agent']} -->\n\n{record['output']}\n")
            if prompt is not None:
                with open(base + ".prompt.md", "w", encoding="utf-8") as f:
                    f.write(prompt)
            self.save()

    def event(self, **ev):
        ev["ts"] = datetime.datetime.now().isoformat(timespec="seconds")
        with self.lock, open(os.path.join(self.dir, "events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

PROTOCOL = """\
---
## Operating protocol (Agency team graph)
You are one node in an automated multi-agent team run by an orchestration
engine, not a chat with a human. Nobody will answer questions mid-task: make
reasonable assumptions, state them briefly, and deliver the complete work
product your task asks for. Upstream teammates' work is provided as context;
build on it rather than redoing it. Be concrete and evidence-based."""


class Engine:
    def __init__(self, lib, catalog, backend, state, max_parallel=4, max_calls=None, log=print):
        self.lib = lib
        self.cat = catalog
        self.backend = backend
        self.state = state
        self.sem = threading.Semaphore(max_parallel)
        self.max_parallel = max_parallel
        self.max_calls = max_calls
        self.calls_made = 0
        self.calls_lock = threading.Lock()
        self.log = log

    # -- entry --------------------------------------------------------------

    def run_graph(self, slug, inputs, path=None):
        g = self.lib.get(slug)
        resolved = {}
        for name, spec in g.get("inputs", {}).items():
            given = inputs.get(name)
            if spec.get("type") == "agent" and given not in (None, "") and given not in self.cat:
                self.log(f"! {path or slug}: '{given}' is not an agent, using default for input '{name}'")
                given = None
            if given not in (None, ""):
                resolved[name] = given
            elif "default" in spec:
                resolved[name] = spec["default"]
            elif spec.get("required"):
                raise GraphError(f"graph '{slug}' requires input '{name}' (pass --input {name}=...)")
            else:
                resolved[name] = ""
        ctx = {"input": resolved, "graph": {"slug": slug, "title": g["title"], "summary": g["summary"]}}
        return self.run_body(g, ctx, path or slug)

    def run_body(self, body, ctx, path):
        """Execute a DAG of nodes with maximal concurrency. Returns a result dict."""
        by_id = {n["id"]: n for n in body["nodes"]}
        results = dict(ctx.get("outer_nodes", {}))
        local = {}
        pending = set(by_id)
        running = {}
        with cf.ThreadPoolExecutor(max_workers=max(1, len(by_id))) as pool:
            while pending or running:
                for nid in list(pending):
                    n = by_id[nid]
                    if all(d in local for d in n.get("after", [])):
                        pending.discard(nid)
                        if "when" in n and not eval_cond(n["when"], results):
                            local[nid] = results[nid] = {"status": "skipped", "output": "(skipped)", "verdict": None}
                            self.log(f"  ⤼ {path}/{nid} skipped ({describe_cond(n['when'])} is false)")
                            self.state.event(path=f"{path}/{nid}", event="skipped")
                            continue
                        node_ctx = dict(ctx, nodes=dict(results))
                        running[pool.submit(self.run_node, n, node_ctx, f"{path}/{nid}")] = nid
                if not running:
                    if pending:  # only reachable when skips unblocked nothing new
                        continue
                    break
                done, _ = cf.wait(running, return_when=cf.FIRST_COMPLETED)
                for fut in done:
                    nid = running.pop(fut)
                    r = fut.result()  # re-raises failures/budget stops
                    local[nid] = results[nid] = r
        outputs = {k: render_value(v, dict(ctx, nodes=results)) for k, v in (body.get("outputs") or {}).items()}
        last = local[list(by_id)[-1]]
        return {
            "status": "done",
            "outputs": outputs,
            "nodes": local,
            "output": outputs.get("result") if isinstance(outputs.get("result"), str) else last.get("output", ""),
            "verdict": outputs.get("verdict") or None,
        }

    def run_node(self, n, ctx, path):
        t = n["type"]
        if t in ("agent", "gate"):
            return self.run_leaf(n, ctx, path)
        if t == "graph":
            inputs = {k: render_value(v, ctx) for k, v in n.get("inputs", {}).items()}
            self.log(f"▶ {path} → team '{n['graph']}'")
            return self.run_graph(n["graph"], inputs, path)
        if t == "loop":
            return self.run_loop(n, ctx, path)
        if t == "map":
            return self.run_map(n, ctx, path)
        raise GraphError(f"{path}: unknown node type {t}")

    def run_composite_body(self, n, ctx, path):
        b = n["body"]
        if "graph" in b:
            inputs = {k: render_value(v, ctx) for k, v in b.get("inputs", {}).items()}
            r = self.run_graph(b["graph"], inputs, path)
            # Expose the whole sub-graph as node "body" for until/break conditions.
            r["nodes"] = dict(r["nodes"], body={"status": r["status"], "verdict": r["verdict"], "output": r["output"]})
            return r
        inner = dict(ctx, outer_nodes=ctx.get("nodes", {}))
        return self.run_body(b, inner, path)

    def run_loop(self, n, ctx, path):
        mx = n["max_iterations"]
        history, last, status = [], None, "exhausted"
        for i in range(1, mx + 1):
            feedback = ""
            if last is not None:
                if "feedback" in n:
                    feedback = render(n["feedback"], dict(ctx, nodes=last["nodes"]))
                else:
                    feedback = "\n\n".join(
                        (last["nodes"].get(u) or {}).get("output", "") for u in cond_nodes(n["until"]))
            loop = {"iteration": i, "max": mx, "feedback": feedback,
                    "history": "\n\n".join(f"### Iteration {j + 1}\n{h}" for j, h in enumerate(history))}
            self.log(f"↻ {path} iteration {i}/{mx}")
            last = self.run_composite_body(n, dict(ctx, loop=loop), f"{path}/iter-{i}")
            history.append(feedback_summary(last, n))
            if eval_cond(n["until"], last["nodes"]):
                status = "passed"
                break
            if "break" in n and eval_cond(n["break"], last["nodes"]):
                status = "broken"
                break
        verdict_node = cond_nodes(n["until"])[0]
        verdict = (last["nodes"].get(verdict_node) or {}).get("verdict")
        self.log(f"{'✔' if status == 'passed' else '✘'} {path} {status} after {i} iteration(s)")
        self.state.event(path=path, event="loop", status=status, iterations=i, verdict=verdict)
        return {"status": status, "iterations": i, "verdict": verdict,
                "output": last.get("output", ""), "outputs": last.get("outputs", {}),
                "nodes": last["nodes"]}

    def run_map(self, n, ctx, path):
        items = n["items"]
        if isinstance(items, str):
            items = lookup(ctx, items)
            if isinstance(items, str):
                try:
                    items = json.loads(items)
                except json.JSONDecodeError:
                    items = None
        if not isinstance(items, list):
            raise GraphError(f"{path}: \"items\" did not resolve to a list (got {type(items).__name__})")
        cap = n.get("max_items", DEFAULT_MAX_ITEMS)
        if len(items) > cap:
            self.log(f"! {path}: {len(items)} items, capped at max_items={cap}")
            items = items[:cap]
        self.log(f"⤵ {path} fan-out over {len(items)} item(s)")
        par = n.get("max_parallel", 1)
        results = [None] * len(items)
        with cf.ThreadPoolExecutor(max_workers=par) as pool:
            futs = {pool.submit(self.run_composite_body, n,
                                dict(ctx, item=it, item_index=idx + 1),
                                f"{path}/item-{idx + 1}"): idx
                    for idx, it in enumerate(items)}
            for fut in cf.as_completed(futs):
                results[futs[fut]] = fut.result()
        verdicts = [r.get("verdict") for r in results]
        ok = n.get("pass_verdicts", DEFAULT_PASS_VERDICTS)
        verdict = "PASS" if all(v is None or v in ok for v in verdicts) else "FAIL"
        parts = []
        for idx, (it, r) in enumerate(zip(items, results)):
            title = it.get("title", it.get("id", "")) if isinstance(it, dict) else str(it)
            parts.append(f"## Item {idx + 1}: {title}\nVerdict: {r.get('verdict') or 'n/a'}\n\n{r.get('output', '')}")
        return {"status": "done", "verdict": verdict, "output": "\n\n".join(parts),
                "json": [{"item": it, "verdict": r.get("verdict"), "outputs": r.get("outputs", {})}
                         for it, r in zip(items, results)],
                "outputs": {"items": len(items), "verdicts": verdicts}}

    # -- leaves ---------------------------------------------------------------

    def resolve_agent(self, n, ctx, path):
        ref = render(n["agent"], ctx).strip()
        if ref not in self.cat:
            fallback = n.get("fallback_agent")
            if fallback and fallback in self.cat:
                self.log(f"! {path}: agent '{ref}' unknown, using fallback '{fallback}'")
                return fallback
            raise GraphError(f"{path}: agent '{ref}' does not exist")
        return ref

    def run_leaf(self, n, ctx, path):
        cached = self.state.get(path)
        if cached:
            self.log(f"  ↺ {path} (cached)")
            return leaf_result(n, cached)
        agent = self.resolve_agent(n, ctx, path)
        req = self.build_request(n, ctx, path, agent)
        self.log(f"  ● {path} · {self.cat.label(agent)}")
        output = self.call(req)
        record = {"agent": agent, "output": output}
        r = leaf_result(n, record)
        if n.get("output") == "json" and r.get("json") is None:
            repair = dict(req, user=req["user"] + "\n\n---\n## Your previous reply\n" + output +
                          "\n\n---\nYour reply did not contain a valid ```json fenced block. "
                          "Reply again with ONLY that JSON block, nothing else.")
            output2 = self.call(repair)
            record = {"agent": agent, "output": output + "\n\n" + output2}
            r = leaf_result(n, record)
            if r.get("json") is None:
                raise GraphError(f"{path}: {agent} did not return parseable JSON")
        if n["type"] == "gate" and r["verdict"] is None:
            self.log(f"! {path}: no VERDICT line found, treating as BLOCKED")
        self.state.put(path, record, req["user"])
        self.state.event(path=path, event="call", agent=agent, verdict=r.get("verdict"))
        if r.get("verdict"):
            self.log(f"    → VERDICT {r['verdict']}")
        return r

    def call(self, req):
        with self.calls_lock:
            if self.max_calls is not None and self.calls_made >= self.max_calls:
                raise BudgetExceeded()
            self.calls_made += 1
        with self.sem:
            return self.backend.call(req)

    def build_request(self, n, ctx, path, agent):
        g = ctx["graph"]
        parts = [f"# Handoff — {g['title']} › {path}", "",
                 "## Team mission", g["summary"], ""]
        inputs = {k: v for k, v in ctx["input"].items() if v not in ("", None)}
        if inputs:
            parts.append("## Run inputs")
            for k, v in inputs.items():
                val = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                parts.append(f"- **{k}**: {val}")
            parts.append("")
        parts += ["## Your task", render(n["task"], ctx).strip(), ""]
        if "item" in ctx:
            it = ctx["item"]
            parts += ["## Work item", it if isinstance(it, str) else "```json\n" + json.dumps(it, ensure_ascii=False, indent=2) + "\n```", ""]
        if "loop" in ctx and ctx["loop"]["iteration"] > 1:
            lp = ctx["loop"]
            parts += [f"## Iteration {lp['iteration']} of {lp['max']}",
                      "The previous iteration did not meet the exit condition. Feedback:", "",
                      lp["feedback"] or "(none)", ""]
        deps = n.get("context", n.get("after", []))
        for d in deps:
            r = ctx["nodes"].get(d)
            if not r or r.get("status") == "skipped":
                continue
            out = r.get("output") or ""
            if len(out) > CONTEXT_CHAR_LIMIT:
                out = out[:CONTEXT_CHAR_LIMIT] + f"\n\n[agency-graph: truncated — full text in the run's nodes/ directory]"
            parts += [f"## Upstream: {d}" + (f" (verdict {r['verdict']})" if r.get("verdict") else ""), out, ""]
        if n["type"] == "gate":
            allowed = n.get("verdicts", DEFAULT_VERDICTS)
            parts += ["## Output contract",
                      "Give your assessment with concrete evidence, then end with exactly one line:",
                      f"`VERDICT: <{'|'.join(allowed)}>`",
                      n.get("verdict_guide", ""), ""]
        if n.get("output") == "json":
            parts += ["## Output contract",
                      "Include your final answer as a single ```json fenced block.",
                      n.get("schema_hint", ""), ""]
        system = self.cat.prompt(agent) + "\n\n" + PROTOCOL
        return {"node": n, "agent": agent, "path": path, "system": system,
                "user": "\n".join(parts).strip() + "\n", "task": render(n["task"], ctx),
                "model": n.get("model"), "effort": n.get("effort")}


def leaf_result(n, record):
    out = record["output"]
    r = {"status": "done", "output": out, "verdict": None, "agent": record["agent"]}
    if n["type"] == "gate":
        allowed = n.get("verdicts", DEFAULT_VERDICTS)
        found = [v.upper() for v in VERDICT_RE.findall(out) if v.upper() in allowed]
        r["verdict"] = found[-1] if found else ("BLOCKED" if "BLOCKED" in allowed else allowed[-1])
    if n.get("output") == "json":
        blocks = JSON_BLOCK_RE.findall(out)
        for b in reversed(blocks):
            try:
                r["json"] = json.loads(b)
                break
            except json.JSONDecodeError:
                continue
        r.setdefault("json", None)
    return r


def feedback_summary(result, n):
    until = cond_nodes(n["until"])
    bits = []
    for u in until:
        r = result["nodes"].get(u) or {}
        bits.append(f"{u}: {r.get('verdict') or r.get('status')}")
    return ", ".join(bits)


# ---------------------------------------------------------------------------
# Presentation: show / mermaid
# ---------------------------------------------------------------------------

def show(lib, cat, slug):
    g = lib.get(slug)
    lines = [f"{g['title']}  [{g['kind']}]  ({slug})", f"  {g['summary']}", ""]
    if g.get("inputs"):
        lines.append("Inputs:")
        for k, s in g["inputs"].items():
            flag = "required" if s.get("required") else f"default: {json.dumps(s.get('default', ''), ensure_ascii=False)}"
            lines.append(f"  {k:<14} {s.get('description', '')} ({flag})")
        lines.append("")
    lines.append("Graph:")

    def walk(body, indent):
        by_id = {n["id"]: n for n in body["nodes"]}
        for nid in topo_order(by_id):
            n = by_id[nid]
            pad = "  " * indent
            after = f"  ← {', '.join(n['after'])}" if n.get("after") else ""
            when = f"  if {describe_cond(n['when'])}" if n.get("when") else ""
            t = n["type"]
            if t in ("agent", "gate"):
                who = n["agent"] if TEMPLATE_RE.search(n["agent"]) else cat.label(n["agent"])
                tag = "◆ gate" if t == "gate" else "● agent"
                extra = f" [{'/'.join(n.get('verdicts', DEFAULT_VERDICTS))}]" if t == "gate" else ""
                lines.append(f"{pad}{tag} {nid}: {who}{extra}{after}{when}")
            elif t == "graph":
                lines.append(f"{pad}▣ team {nid}: {n['graph']}{after}{when}")
            elif t == "loop":
                lines.append(f"{pad}↻ loop {nid}: ≤{n['max_iterations']}× until {describe_cond(n['until'])}"
                             + (f", break if {describe_cond(n['break'])}" if n.get("break") else "") + after + when)
                body_(n["body"], indent + 1)
            elif t == "map":
                src = n["items"] if isinstance(n["items"], str) else f"{len(n['items'])} literal items"
                lines.append(f"{pad}⤵ map {nid}: for each in {src} (≤{n.get('max_items', DEFAULT_MAX_ITEMS)}, ∥{n.get('max_parallel', 1)}){after}{when}")
                body_(n["body"], indent + 1)

    def body_(b, indent):
        if "graph" in b:
            lines.append("  " * indent + f"▣ team: {b['graph']}")
        else:
            walk(b, indent)

    walk(g, 1)
    lines += ["", f"Agents ({len(roster(lib, g))}): " + ", ".join(roster(lib, g)),
              f"Worst-case agent calls: {graph_bound(lib, slug, {})}"]
    return "\n".join(lines)


def mermaid(lib, cat, slug, expand=False):
    out = ["flowchart TD"]
    counter = [0]

    def mid(prefix, nid):
        return re.sub(r"[^A-Za-z0-9_]", "_", f"{prefix}_{nid}")

    def esc(s):
        return TEMPLATE_RE.sub(lambda m: "‹" + m.group(1) + "›", s).replace('"', "'")

    def emit_body(body, prefix, indent, depth):
        pad = "  " * indent
        by_id = {n["id"]: n for n in body["nodes"]}
        for nid in topo_order(by_id):
            n = by_id[nid]
            me = mid(prefix, nid)
            t = n["type"]
            if t in ("agent", "gate"):
                who = n["agent"] if TEMPLATE_RE.search(n["agent"]) else cat.label(n["agent"])
                if t == "gate":
                    out.append(f'{pad}{me}{{"{esc(nid)}<br/>{esc(who)}"}}')
                else:
                    out.append(f'{pad}{me}["{esc(nid)}<br/>{esc(who)}"]')
            elif t == "graph":
                if expand and depth < 4:
                    out.append(f'{pad}subgraph {me}["▣ {esc(nid)}: {esc(lib.get(n["graph"])["title"])}"]')
                    emit_body(lib.get(n["graph"]), me, indent + 1, depth + 1)
                    out.append(f"{pad}end")
                else:
                    out.append(f'{pad}{me}[["▣ {esc(nid)}<br/>team: {esc(n["graph"])}"]]')
            else:
                if t == "loop":
                    label = f"↻ {nid}: ≤{n['max_iterations']}× until {describe_cond(n['until'])}"
                else:
                    src = n["items"] if isinstance(n["items"], str) else "list"
                    label = f"⤵ {nid}: for each {src} (∥{n.get('max_parallel', 1)})"
                out.append(f'{pad}subgraph {me}["{esc(label)}"]')
                b = n["body"]
                if "graph" in b:
                    if expand and depth < 4:
                        emit_body(lib.get(b["graph"]), me + "_b", indent + 1, depth + 1)
                    else:
                        out.append(f'{pad}  {me}_body[["team: {esc(b["graph"])}"]]')
                else:
                    emit_body(b, me, indent + 1, depth + 1)
                    if t == "loop":
                        by2 = {x["id"]: x for x in b["nodes"]}
                        order2 = topo_order(by2)
                        if order2 and len(order2) > 1:
                            out.append(f"{pad}  {mid(me, order2[-1])} -. next iteration .-> {mid(me, order2[0])}")
                out.append(f"{pad}end")
            for d in n.get("after", []):
                label = f"|{esc(describe_cond(n['when']))}|" if n.get("when") else ""
                arrow = "-.->" if n.get("when") else "-->"
                out.append(f"{pad}{mid(prefix, d)} {arrow}{label} {me}")

    emit_body(lib.get(slug), "g", 1, 0)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_inputs(pairs, file_pairs):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise GraphError(f"--input expects key=value, got '{p}'")
        k, v = p.split("=", 1)
        v2 = v.strip()
        if v2[:1] in "[{":
            try:
                v = json.loads(v2)
            except json.JSONDecodeError:
                pass
        out[k] = v
    for p in file_pairs or []:
        k, fpath = p.split("=", 1)
        with open(fpath, encoding="utf-8") as f:
            out[k] = f.read()
    return out


def parse_mock_verdicts(pairs):
    out = {}
    for p in pairs or []:
        k, v = p.split("=", 1)
        out[k] = [x.strip().upper() for x in v.split(",") if x.strip()]
    return out


def write_report(state, slug, result, lib):
    g = lib.get(slug)
    lines = [f"# {g['title']} — run report", "",
             f"- Graph: `{slug}`", f"- Backend: `{state.meta.get('backend')}`",
             f"- Started: {state.meta.get('started')}", f"- Agent calls: {len(state.calls)}",
             f"- Final verdict: **{result.get('verdict') or 'n/a'}**", "", "## Outputs", ""]
    for k, v in (result.get("outputs") or {}).items():
        val = v if isinstance(v, str) else "```json\n" + json.dumps(v, ensure_ascii=False, indent=2) + "\n```"
        lines += [f"### {k}", "", val, ""]
    lines += ["## Call log", "", "| Node | Agent | Verdict |", "|---|---|---|"]
    for path, rec in state.calls.items():
        v = ""
        m = VERDICT_RE.findall(rec["output"])
        if m:
            v = m[-1].upper()
        lines.append(f"| `{path}` | {rec['agent']} | {v} |")
    with open(os.path.join(state.dir, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def cmd_run(args, lib, cat):
    if args.resume:
        state = RunState(args.resume)
        meta = state.meta
        slug = meta["graph"] if meta["graph"] in lib.graphs else lib.resolve(meta["graph_path"])
        inputs = meta["inputs"]
        backend_name = args.backend or meta["backend"]
        workdir = meta.get("workdir") or os.getcwd()
    else:
        if not args.graph:
            raise GraphError("run needs a graph (or --resume <run-dir>)")
        slug = lib.resolve(args.graph)
        inputs = parse_inputs(args.input, args.input_file)
        backend_name = args.backend or "mock"
        workdir = os.path.abspath(args.workdir or os.getcwd())
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = args.run_dir or os.path.join(workdir, ".agency-runs", f"{slug}-{stamp}")
        state = RunState(run_dir, {"graph": slug, "graph_path": lib.paths.get(slug), "inputs": inputs,
                                   "backend": backend_name, "workdir": workdir,
                                   "started": datetime.datetime.now().isoformat(timespec="seconds")})
    errs = Validator(lib, cat).validate_all()
    if errs:
        raise GraphError("graph library is invalid:\n  " + "\n  ".join(errs))
    if backend_name == "mock":
        backend = MockBackend(parse_mock_verdicts(args.mock_verdict))
    elif backend_name == "claude-code":
        backend = ClaudeCodeBackend(workdir, args.claude_args, args.model)
    elif backend_name == "anthropic":
        backend = AnthropicBackend(args.model, args.effort)
    else:
        raise GraphError(f"unknown backend '{backend_name}'")
    quiet = args.quiet
    log_lock = threading.Lock()

    def log(*a):
        if not quiet:
            with log_lock:
                print(*a, flush=True)

    engine = Engine(lib, cat, backend, state, args.max_parallel, args.max_calls,
                    log=log)
    print(f"Running '{slug}' on backend '{backend_name}' → {state.dir}", flush=True)
    try:
        result = engine.run_graph(slug, inputs)
    except BudgetExceeded:
        print(f"\n⏸  --max-calls={args.max_calls} reached. Checkpoint saved; continue with:\n"
              f"   scripts/agency_graph.py run --resume {state.dir}")
        return 3
    except KeyboardInterrupt:
        print(f"\n⏸  interrupted. Continue with: scripts/agency_graph.py run --resume {state.dir}")
        return 130
    write_report(state, slug, result, lib)
    state.meta["finished"] = datetime.datetime.now().isoformat(timespec="seconds")
    state.meta["verdict"] = result.get("verdict")
    state.save()
    print(f"\n✅ done — verdict: {result.get('verdict') or 'n/a'} · "
          f"{engine.calls_made} new agent call(s) · report: {os.path.join(state.dir, 'report.md')}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="agency_graph.py",
                                 description="Compose Agency agents into automated team graphs with loops.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list team graphs and building blocks")
    p = sub.add_parser("validate", help="statically check graphs (default: all)")
    p.add_argument("graphs", nargs="*")
    p = sub.add_parser("show", help="print a graph's structure, roster and call bound")
    p.add_argument("graph")
    p = sub.add_parser("mermaid", help="render a graph as a Mermaid flowchart")
    p.add_argument("graph")
    p.add_argument("--expand", action="store_true", help="inline sub-team graphs")
    p = sub.add_parser("roster", help="agent slugs a graph uses (feed to install.sh --agent)")
    p.add_argument("graph")
    p.add_argument("--csv", action="store_true")
    p = sub.add_parser("run", help="execute a graph")
    p.add_argument("graph", nargs="?")
    p.add_argument("--input", "-i", action="append", metavar="KEY=VALUE")
    p.add_argument("--input-file", action="append", metavar="KEY=PATH", help="read an input value from a file")
    p.add_argument("--backend", choices=["mock", "claude-code", "anthropic"])
    p.add_argument("--model", help="model for claude-code/anthropic (anthropic default: claude-opus-5-5)")
    p.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="anthropic backend effort (default high)")
    p.add_argument("--claude-args", default="", help="extra flags for `claude -p`, e.g. \"--permission-mode acceptEdits\"")
    p.add_argument("--workdir", help="project directory agents work in (default: cwd)")
    p.add_argument("--run-dir", help="where to write state/report (default: <workdir>/.agency-runs/<graph>-<ts>)")
    p.add_argument("--resume", metavar="RUN_DIR", help="continue a checkpointed run")
    p.add_argument("--max-parallel", type=int, default=4, help="concurrent agent calls (default 4)")
    p.add_argument("--max-calls", type=int, help="stop (resumably) after this many new agent calls")
    p.add_argument("--mock-verdict", action="append", metavar="NODE=V1,V2", help="mock backend: scripted gate verdicts")
    p.add_argument("--quiet", "-q", action="store_true")
    args = ap.parse_args(argv)

    cat = Catalog()
    lib = Library()
    try:
        if args.cmd == "list":
            for kind in ("team", "block"):
                print("Teams (runnable):" if kind == "team" else "\nBuilding blocks (sub-teams):")
                for s, g in sorted(lib.graphs.items()):
                    if g.get("kind") == kind:
                        print(f"  {s:<26} {g.get('summary', '')}")
            return 0
        if args.cmd == "validate":
            slugs = [lib.resolve(x) for x in args.graphs] or None
            errs = Validator(lib, cat).validate_all(slugs)
            if errs:
                print(f"FAILED: {len(errs)} graph error(s).\n")
                for e in errs:
                    print(f"  ERROR {e}")
                return 1
            checked = slugs or sorted(lib.graphs)
            for s in checked:
                print(f"  ok  {s:<26} ≤{graph_bound(lib, s, {})} agent calls, {len(roster(lib, lib.get(s)))} agents")
            print(f"PASSED: {len(checked)} graph(s) valid — every agent resolves, every loop is bounded, no recursion.")
            return 0
        slug = lib.resolve(args.graph) if getattr(args, "graph", None) else None
        if args.cmd in ("show", "mermaid", "roster"):
            errs = Validator(lib, cat).validate_all()
            if errs:
                raise GraphError("graph library is invalid (run `validate`):\n  " + "\n  ".join(errs))
        if args.cmd == "show":
            print(show(lib, cat, slug))
        elif args.cmd == "mermaid":
            print(mermaid(lib, cat, slug, args.expand))
        elif args.cmd == "roster":
            r = roster(lib, lib.get(slug))
            print(",".join(r) if args.csv else "\n".join(r))
        elif args.cmd == "run":
            return cmd_run(args, lib, cat)
        return 0
    except GraphError as e:
        print(f"ERROR {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
