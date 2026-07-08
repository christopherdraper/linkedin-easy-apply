"""Command-line entry point for the Q2 assisted apply agent."""

import argparse

from q2apply.queue_runner import _get_q2_pending, process_by_id, process_next_pending


def main():
    parser = argparse.ArgumentParser(description="Q2 Assisted Apply (MCP Playwright)")
    parser.add_argument("--job-id", help="Process a specific job by ID")
    parser.add_argument("--list", action="store_true", help="List pending Q2 entries")
    parser.add_argument("--max", type=int, default=1, help="Max entries to process (default: 1)")
    parser.add_argument("--proxy", help="Proxy server URL")
    args = parser.parse_args()

    if args.list:
        pending = _get_q2_pending()
        if not pending:
            print("No pending Q2 entries.")
            return
        print(f"\n{'=' * 80}")
        print(f"  Q2 Pending: {len(pending)} entries")
        print(f"{'=' * 80}\n")
        for q in pending:
            score_pct = int((q.get("match_score") or 0) * 100)
            attempts = q.get("q2_attempts", 0)
            print(
                f"  [{q['job_id']}] {q.get('company', '?')} - {q.get('title', '?')}"
                f"  (score: {score_pct}%, attempts: {attempts})"
            )
        return

    if args.job_id:
        status = process_by_id(args.job_id, proxy=args.proxy)
        print(f"Result: {status}")
        return

    results = process_next_pending(proxy=args.proxy, max_count=args.max)
    if not results:
        print("No pending Q2 entries to process.")
        return

    print(f"\nProcessed {len(results)} entries:")
    for r in results:
        print(f"  [{r['job_id']}] {r['status']}")
