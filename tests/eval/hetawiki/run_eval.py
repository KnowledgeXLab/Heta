"""HetaWiki evaluation runner.

Pipeline:
  1. Ingest test documents (first 2 default, remaining merge)
  2. Poll until all ingest tasks complete
  3. Check page content (ingest_content cases)
  4. Run wiki queries (query cases)
  5. Check graph edges (graph_edge cases)
  6. Write markdown + JSON report

Usage:
    python run_eval.py [--base-url http://localhost:8000] [--llm-judge] [--skip-ingest]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml

# ── Constants ─────────────────────────────────────────────────────────────────

EVAL_DIR     = Path(__file__).parent
DATASETS_DIR = EVAL_DIR / "datasets"
GT_FILE      = EVAL_DIR / "ground_truth.yaml"
REPORT_DIR   = EVAL_DIR / "report"
CONFIG_FILE  = EVAL_DIR.parent.parent.parent / "config.yaml"

TERMINAL       = {"completed", "failed", "cancelled"}
POLL_INTERVAL  = 3    # seconds
INGEST_TIMEOUT = 600  # seconds per task

# Ingest order: first N files use default mode, rest use merge
DEFAULT_FILES = ["gradient_descent.md", "backpropagation.md"]
MERGE_FILES   = ["neural_network.md", "overfitting.md", "regularization.md"]


# ── API client ────────────────────────────────────────────────────────────────

class WikiClient:
    def __init__(self, base_url: str):
        self.base = base_url.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def ingest(self, file_path: Path, merge: bool) -> dict:
        with open(file_path, "rb") as f:
            r = requests.post(
                self._url("/api/v1/hetawiki/ingest"),
                files={"file": (file_path.name, f, "text/markdown")},
                data={"merge": str(merge).lower()},
                timeout=30,
            )
        r.raise_for_status()
        return r.json()

    def get_task(self, task_id: str) -> dict:
        r = requests.get(self._url(f"/api/v1/hetawiki/tasks/{task_id}"), timeout=10)
        r.raise_for_status()
        return r.json()

    def wait_for_task(self, task_id: str, label: str) -> dict:
        deadline = time.time() + INGEST_TIMEOUT
        while time.time() < deadline:
            task = self.get_task(task_id)
            status = task.get("status", "unknown")
            msg    = task.get("message", "")
            print(f"    [{label}] {status} — {msg}", flush=True)
            if status in TERMINAL:
                return task
            time.sleep(POLL_INTERVAL)
        return {"status": "timeout", "error": "timed out"}

    def get_page(self, stem: str) -> dict | None:
        try:
            r = requests.get(self._url(f"/api/v1/hetawiki/pages/{stem}"), timeout=10)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    def get_graph(self) -> dict:
        r = requests.get(self._url("/api/v1/hetawiki/graph"), timeout=10)
        r.raise_for_status()
        return r.json()

    def query(self, question: str) -> dict:
        r = requests.post(
            self._url("/api/v1/hetawiki/query"),
            json={"question": question},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        task_id = data["task_id"]
        # Poll until query task completes
        deadline = time.time() + 300
        while time.time() < deadline:
            task = self.get_task(task_id)
            if task.get("status") == "completed":
                result = task.get("result") or {}
                return {"answer": result.get("answer", ""), "sources": result.get("sources", [])}
            if task.get("status") in {"failed", "cancelled"}:
                return {"answer": "", "error": task.get("error", "failed")}
            time.sleep(POLL_INTERVAL)
        return {"answer": "", "error": "query timed out"}


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_keywords(text: str, keywords: list[str]) -> tuple[bool, list[str]]:
    text_lower = text.lower()
    missing = [kw for kw in keywords if kw.lower() not in text_lower]
    return len(missing) == 0, missing


def score_llm_judge(question: str, expected: str, actual: str) -> tuple[int, str]:
    try:
        from openai import OpenAI
    except ImportError:
        return -1, "openai not installed"
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        llm_cfg = cfg.get("hetawiki", {}).get("llm", {})
        if not llm_cfg.get("api_key"):
            return -1, "no api_key in hetawiki.llm config"
    except Exception as e:
        return -1, f"config error: {e}"

    if not actual.strip():
        return 0, "empty response"

    prompt = (
        "You are an evaluation judge for a wiki query system.\n"
        "Score the answer 0–5 based on factual correctness.\n\n"
        "CRITICAL: base your score ONLY on the model answer. "
        "Do NOT use your own knowledge to fill gaps.\n\n"
        f"Question: {question}\n"
        f"Expected: {expected}\n"
        f"Model answer: {actual.strip()}\n\n"
        "5=fully correct, 4=mostly correct, 3=partially correct, "
        "2=tangential, 1=incorrect but on-topic, 0=wrong/empty.\n\n"
        "Respond with ONLY:\nSCORE: <0-5>\nREASON: <one sentence>"
    )
    try:
        import httpx
        client = OpenAI(
            api_key=llm_cfg["api_key"],
            base_url=llm_cfg["base_url"],
            timeout=60,
            http_client=httpx.Client(trust_env=False),  # bypass SOCKS/system proxy
        )
        resp = client.chat.completions.create(
            model=llm_cfg.get("model", "qwen3-max"),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=128,
            temperature=0,
            extra_body={"enable_thinking": False},
        )
        text = resp.choices[0].message.content or ""
        m = re.search(r"SCORE:\s*([0-5])", text)
        score = int(m.group(1)) if m else -1
        m2 = re.search(r"REASON:\s*(.+)", text, re.DOTALL)
        reason = m2.group(1).strip().splitlines()[0] if m2 else text[:200]
        return score, reason
    except Exception as e:
        return -1, f"LLM call error: {e}"


# ── Report ────────────────────────────────────────────────────────────────────

def generate_report(results: list[dict], use_llm_judge: bool) -> str:
    total     = len(results)
    kw_pass   = sum(1 for r in results if r.get("kw_pass", False))
    kw_fail   = total - kw_pass

    lines = [
        "# HetaWiki Evaluation Report",
        f"\nGenerated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "\n## Summary\n",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total cases | {total} |",
        f"| Pass | {kw_pass} ({kw_pass/total*100:.1f}%) |",
        f"| Fail | {kw_fail} ({kw_fail/total*100:.1f}%) |",
    ]

    if use_llm_judge:
        judged = [r for r in results if r.get("llm_score", -1) >= 0]
        if judged:
            avg = sum(r["llm_score"] for r in judged) / len(judged)
            lines.append(f"| LLM judge avg (0-5) | {avg:.2f} |")

    # By eval type
    for etype in ("ingest_content", "query", "graph_edge"):
        group = [r for r in results if r.get("eval_type") == etype]
        if not group:
            continue
        g_pass = sum(1 for r in group if r.get("kw_pass", False))
        lines.append(f"\n### {etype}  ({g_pass}/{len(group)} pass)\n")
        lines.append("| ID | Pass | Missing / Note | LLM score |")
        lines.append("|----|------|---------------|-----------|")
        for r in group:
            icon    = "✅" if r.get("kw_pass") else "❌"
            missing = ", ".join(r.get("missing_kw", [])) or "—"
            llm     = str(r.get("llm_score", "—"))
            lines.append(f"| {r['id']} | {icon} | {missing} | {llm} |")

    # Failure details
    failures = [r for r in results if not r.get("kw_pass")]
    if failures:
        lines.append("\n## Failure Details\n")
        for r in failures:
            lines.append(f"### {r['id']} ({r.get('eval_type')})")
            if r.get("question"):
                lines.append(f"**Question:** {r['question']}\n")
            if r.get("description"):
                lines.append(f"**Description:** {r['description']}\n")
            lines.append(f"**Missing keywords:** {', '.join(r.get('missing_kw', []))}\n")
            if r.get("actual"):
                snippet = r["actual"][:400]
                lines.append(f"**Actual (truncated):**\n```\n{snippet}\n```\n")
            if r.get("llm_reason"):
                lines.append(f"**LLM reason:** {r['llm_reason']}\n")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="HetaWiki eval runner")
    parser.add_argument("--base-url",    default="http://localhost:8000")
    parser.add_argument("--skip-ingest", action="store_true",
                        help="Skip ingest; assume wiki already populated")
    parser.add_argument("--llm-judge",   action="store_true",
                        help="Run LLM judge on query results")
    args = parser.parse_args()

    client = WikiClient(args.base_url)
    with open(GT_FILE, encoding="utf-8") as f:
        ground_truth: list[dict] = yaml.safe_load(f)

    print(f"\n{'='*60}")
    print(f"HetaWiki Eval  |  {len(ground_truth)} cases")
    print(f"{'='*60}\n")

    # ── Step 1: Ingest ────────────────────────────────────────

    ingested_pages: dict[str, str] = {}   # filename → wiki page stem (guess)

    if not args.skip_ingest:
        print("Step 1: Ingesting test documents…\n")

        all_files = [(f, False) for f in DEFAULT_FILES] + [(f, True) for f in MERGE_FILES]

        for filename, merge in all_files:
            path = DATASETS_DIR / filename
            if not path.exists():
                print(f"  SKIP (not found): {filename}")
                continue
            mode_label = "merge" if merge else "default"
            print(f"  [{mode_label}] {filename}")
            try:
                resp    = client.ingest(path, merge=merge)
                task_id = resp["task_id"]
                result  = client.wait_for_task(task_id, filename)
                if result.get("status") == "completed":
                    title = (result.get("result") or {}).get("title", "")
                    print(f"    ✅ completed  title={title!r}\n")
                    ingested_pages[filename] = title
                else:
                    print(f"    ❌ {result.get('status')}  {result.get('error', '')}\n")
            except Exception as e:
                print(f"    ERROR: {e}\n")
    else:
        print("Step 1: Skipped (--skip-ingest)\n")

    # ── Step 2: Fetch graph once ──────────────────────────────

    print("Step 2: Fetching wiki graph…")
    try:
        graph  = client.get_graph()
        g_nodes: list[dict] = graph.get("nodes", [])
        g_edges: list[dict] = graph.get("edges", [])
        print(f"  {len(g_nodes)} pages, {len(g_edges)} edges\n")
    except Exception as e:
        print(f"  ERROR fetching graph: {e}\n")
        g_nodes, g_edges = [], []

    # ── Step 3: Run eval cases ────────────────────────────────

    print("Step 3: Running eval cases…\n")
    eval_results: list[dict] = []

    for i, gt in enumerate(ground_truth, 1):
        eid       = gt["id"]
        etype     = gt["eval_type"]
        keywords  = gt.get("keywords", [])
        difficulty = gt.get("difficulty", "")
        print(f"  [{i:02d}/{len(ground_truth)}] {eid}  ({etype}, {difficulty})")

        result: dict = {"id": eid, "eval_type": etype, "difficulty": difficulty}

        # ── ingest_content ──
        if etype == "ingest_content":
            filename = gt["file"]
            result["description"] = gt.get("description", "")

            page_content = ""

            # 1. Use the title recorded at ingest time to find the exact node
            known_title = ingested_pages.get(filename, "").lower()
            if known_title:
                for node in g_nodes:
                    if node.get("title", "").lower() == known_title:
                        stem = node["id"].replace("pages/", "")
                        page = client.get_page(stem)
                        if page:
                            page_content = page["content"]
                        break

            # 2. Fallback: match node title against filename stem (no content search)
            if not page_content:
                file_stem = filename.replace(".md", "").replace("_", " ").lower()
                for node in g_nodes:
                    node_title = node.get("title", "").lower().replace("-", " ")
                    if file_stem in node_title or node_title in file_stem:
                        stem = node["id"].replace("pages/", "")
                        page = client.get_page(stem)
                        if page:
                            page_content = page["content"]
                        break

            # 3. Last resort: direct stem guess
            if not page_content:
                for stem_guess in [
                    filename.replace(".md", "").replace("_", "-"),
                    filename.replace(".md", "").replace("_", "-").capitalize(),
                ]:
                    page = client.get_page(stem_guess)
                    if page:
                        page_content = page["content"]
                        break

            result["actual"]      = page_content[:600] if page_content else ""
            kw_pass, missing      = score_keywords(page_content, keywords)
            result["kw_pass"]     = kw_pass
            result["missing_kw"]  = missing

        # ── query ──
        elif etype == "query":
            question         = gt["question"]
            result["question"] = question
            result["expected"] = gt.get("expected", "")
            print(f"    Q: {question[:70]}…")
            try:
                resp            = client.query(question)
                answer          = resp.get("answer", "")
                result["actual"] = answer
                print(f"    A: {answer[:120]}…")
            except Exception as e:
                result["actual"] = f"ERROR: {e}"
                print(f"    ERROR: {e}")

            kw_pass, missing     = score_keywords(result["actual"], keywords)
            result["kw_pass"]    = kw_pass
            result["missing_kw"] = missing

            if args.llm_judge and result["actual"]:
                score, reason          = score_llm_judge(question, result["expected"], result["actual"])
                result["llm_score"]    = score
                result["llm_reason"]   = reason
                print(f"    LLM judge: {score}/5 — {reason[:80]}")

        # ── graph_edge ──
        elif etype == "graph_edge":
            pa_substr = gt["page_a_title_contains"].lower()
            pb_substr = gt["page_b_title_contains"].lower()
            result["description"] = gt.get("description", "")

            # Find matching node IDs
            def find_node(substr: str) -> str | None:
                for n in g_nodes:
                    if substr in n.get("title", "").lower() or substr in n["id"].lower():
                        return n["id"]
                return None

            id_a = find_node(pa_substr)
            id_b = find_node(pb_substr)

            if not id_a or not id_b:
                result["kw_pass"]    = False
                result["missing_kw"] = [f"page not found: {pa_substr if not id_a else pb_substr}"]
                result["actual"]     = f"id_a={id_a}, id_b={id_b}"
            else:
                edge_exists = any(
                    (e["source"] == id_a and e["target"] == id_b) or
                    (e["source"] == id_b and e["target"] == id_a)
                    for e in g_edges
                )
                result["kw_pass"]    = edge_exists
                result["missing_kw"] = [] if edge_exists else [f"no edge between {id_a} and {id_b}"]
                result["actual"]     = f"edge_exists={edge_exists}  ({id_a} ↔ {id_b})"

        else:
            result["kw_pass"]    = False
            result["missing_kw"] = [f"unknown eval_type: {etype}"]
            result["actual"]     = ""

        icon = "✅" if result.get("kw_pass") else "❌"
        print(f"    {icon}  missing={result.get('missing_kw') or 'none'}\n")
        eval_results.append(result)

    # ── Step 4: Report ────────────────────────────────────────

    REPORT_DIR.mkdir(exist_ok=True)
    ts          = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"eval_{ts}.md"
    json_path   = REPORT_DIR / f"eval_{ts}.json"

    report_md = generate_report(eval_results, use_llm_judge=args.llm_judge)
    report_path.write_text(report_md, encoding="utf-8")
    json_path.write_text(json.dumps(eval_results, ensure_ascii=False, indent=2), encoding="utf-8")

    total   = len(eval_results)
    kw_pass = sum(1 for r in eval_results if r.get("kw_pass"))
    print(f"{'='*60}")
    print(f"DONE — {kw_pass}/{total} pass ({kw_pass/total*100:.1f}%)")
    print(f"Report : {report_path}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
