"""Tests for scripts/agency_graph.py — run: python3 -m unittest discover -s scripts/tests"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import agency_graph as ag  # noqa: E402

CAT = ag.Catalog()


def lib_with(*graphs):
    """The real library plus extra in-memory graphs."""
    lib = ag.Library()
    for g in graphs:
        lib.graphs[g["slug"]] = g
        lib.paths[g["slug"]] = g["slug"] + ".json"
    return lib


def run(lib, slug, inputs=None, verdicts=None, max_calls=None, run_dir=None):
    d = run_dir or tempfile.mkdtemp()
    state = ag.RunState(d, {"graph": slug})
    eng = ag.Engine(lib, CAT, ag.MockBackend(verdicts), state, max_calls=max_calls, log=lambda *a: None)
    return eng.run_graph(slug, inputs or {}), state, eng


def graph(slug, nodes, **extra):
    return dict({"slug": slug, "title": slug, "kind": "team", "summary": "test", "nodes": nodes}, **extra)


class LibraryTest(unittest.TestCase):
    def test_shipped_library_is_valid(self):
        self.assertEqual(ag.Validator(ag.Library(), CAT).validate_all(), [])

    def test_every_graph_has_a_finite_call_bound(self):
        lib = ag.Library()
        for slug in lib.graphs:
            self.assertGreater(ag.graph_bound(lib, slug, {}), 0, slug)


class ValidationTest(unittest.TestCase):
    def errors(self, *graphs):
        lib = lib_with(*graphs)
        return ag.Validator(lib, CAT).validate_all([g["slug"] for g in graphs])

    def test_unknown_agent(self):
        e = self.errors(graph("t", [{"id": "a", "type": "agent", "agent": "nobody", "task": "x"}]))
        self.assertTrue(any("nobody" in x for x in e))

    def test_dag_cycle_rejected(self):
        e = self.errors(graph("t", [
            {"id": "a", "type": "agent", "agent": "engineering-code-reviewer", "task": "x", "after": ["b"]},
            {"id": "b", "type": "agent", "agent": "engineering-code-reviewer", "task": "x", "after": ["a"]}]))
        self.assertTrue(any("cycle" in x for x in e))

    def test_unbounded_loop_rejected(self):
        e = self.errors(graph("t", [{"id": "l", "type": "loop", "until": {"node": "g", "verdict": "PASS"},
                                     "body": {"nodes": [{"id": "g", "type": "gate", "agent": "testing-reality-checker", "task": "x"}]}}]))
        self.assertTrue(any("max_iterations" in x for x in e))

    def test_template_must_reference_upstream(self):
        e = self.errors(graph("t", [
            {"id": "a", "type": "agent", "agent": "engineering-code-reviewer", "task": "{{nodes.b.output}}"},
            {"id": "b", "type": "agent", "agent": "engineering-code-reviewer", "task": "x"}]))
        self.assertTrue(any("not upstream" in x for x in e))

    def test_impossible_verdict_rejected(self):
        e = self.errors(graph("t", [
            {"id": "g", "type": "gate", "agent": "testing-reality-checker", "task": "x", "verdicts": ["READY", "NEEDS_WORK"]},
            {"id": "a", "type": "agent", "agent": "engineering-code-reviewer", "task": "x", "after": ["g"],
             "when": {"node": "g", "verdict": "PASS"}}]))
        self.assertTrue(any("can never return" in x for x in e))

    def test_recursive_composition_rejected(self):
        a = graph("ra", [{"id": "x", "type": "graph", "graph": "rb"}])
        b = graph("rb", [{"id": "y", "type": "graph", "graph": "ra"}])
        self.assertTrue(any("recurse" in x for x in self.errors(a, b)))


class EngineTest(unittest.TestCase):
    def test_dev_qa_loop_retries_then_passes(self):
        r, state, _ = run(ag.Library(), "dev-qa-loop", {"task": "t"}, {"qa": ["FAIL", "FAIL", "PASS"]})
        self.assertEqual(r["verdict"], "PASS")
        self.assertEqual(r["nodes"]["attempts"]["iterations"], 3)
        self.assertEqual(r["nodes"]["escalate"]["status"], "skipped")

    def test_dev_qa_loop_escalates_when_exhausted(self):
        r, state, _ = run(ag.Library(), "dev-qa-loop", {"task": "t"}, {"qa": ["FAIL"]})
        self.assertEqual(r["nodes"]["attempts"]["status"], "exhausted")
        self.assertEqual(r["nodes"]["escalate"]["status"], "done")
        self.assertEqual(len(state.calls), 7)

    def test_break_verdict_stops_loop(self):
        r, _, _ = run(ag.Library(), "dev-qa-loop", {"task": "t"}, {"qa": ["BLOCKED"]})
        self.assertEqual(r["nodes"]["attempts"]["status"], "broken")
        self.assertEqual(r["nodes"]["attempts"]["iterations"], 1)

    def test_feedback_reaches_next_iteration(self):
        _, state, _ = run(ag.Library(), "dev-qa-loop", {"task": "t"}, {"qa": ["FAIL", "PASS"]})
        prompt_file = os.path.join(state.dir, "nodes", "dev-qa-loop__attempts__iter-2__implement.prompt.md")
        with open(prompt_file) as f:
            prompt = f.read()
        self.assertIn("Iteration 2 of 3", prompt)
        self.assertIn("VERDICT: FAIL", prompt)

    def test_map_fans_out_with_per_item_agents(self):
        r, state, _ = run(ag.Library(), "feature-delivery", {"spec": "s"})
        agents = {rec["agent"] for p, rec in state.calls.items() if "/build/" in p}
        self.assertIn("engineering-backend-architect", agents)
        self.assertIn("testing-api-tester", agents)
        self.assertEqual(r["verdict"], "READY")

    def test_when_branches(self):
        r, _, _ = run(ag.Library(), "startup-mvp", {"idea": "i"},
                      {"check": ["NEEDS_WORK"]})
        self.assertEqual(r["nodes"]["launch"]["status"], "skipped")
        self.assertEqual(r["nodes"]["recovery"]["status"], "done")

    def test_unknown_dynamic_agent_falls_back_to_default(self):
        g = graph("dyn", [{"id": "m", "type": "map", "items": [{"dev": "not-an-agent"}],
                           "body": {"graph": "dev-qa-loop", "inputs": {"task": "t", "developer": "{{item.dev}}"}}}])
        _, state, _ = run(lib_with(g), "dyn")
        self.assertIn("engineering-senior-developer", {rec["agent"] for rec in state.calls.values()})

    def test_budget_stop_and_resume(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(ag.BudgetExceeded):
            run(ag.Library(), "feature-delivery", {"spec": "s"}, max_calls=3, run_dir=d)
        _, state, eng = run(ag.Library(), "feature-delivery", {"spec": "s"}, run_dir=d)
        self.assertEqual(eng.calls_made + 3, len(state.calls))

    def test_json_repair_call(self):
        class Forgetful(ag.MockBackend):
            n = 0

            def call(self, req):
                self.n += 1
                return "no json here" if self.n == 1 else '```json\n[{"id": "A"}]\n```'
        g = graph("js", [{"id": "p", "type": "agent", "agent": "project-manager-senior", "task": "x", "output": "json"}])
        state = ag.RunState(tempfile.mkdtemp(), {})
        eng = ag.Engine(lib_with(g), CAT, Forgetful(), state, log=lambda *a: None)
        r = eng.run_graph("js", {})
        self.assertEqual(r["nodes"]["p"]["json"], [{"id": "A"}])


class PresentationTest(unittest.TestCase):
    def test_mermaid_and_show_render(self):
        lib = ag.Library()
        for slug in lib.graphs:
            self.assertTrue(ag.mermaid(lib, CAT, slug, expand=True).startswith("flowchart TD"))
            self.assertIn("Worst-case agent calls", ag.show(lib, CAT, slug))

    def test_roster_includes_nested_and_pool_agents(self):
        lib = ag.Library()
        r = ag.roster(lib, lib.get("startup-mvp"))
        for slug in ("product-trend-researcher", "testing-reality-checker", "testing-api-tester", "design-brand-guardian"):
            self.assertIn(slug, r)


if __name__ == "__main__":
    unittest.main()
