#!/usr/bin/env python3
"""MARKO CLI entry point.

Use this for local command-line operations:
    python cli.py <command> [args...]

main.py is reserved as the Vercel/WSGI entrypoint and only exposes the Flask
app; it intentionally has no CLI behavior.
"""
import sys
import commands
import scraper


HELP_TEXT = """MARKO CLI

Usage:
  python cli.py run <name> <project>
  python cli.py add_lead <name> <email> <niche>
  python cli.py send [--dry-run]
  python cli.py log <count> [opens] [replies] [signups]
  python cli.py analyze
  python cli.py report
  python cli.py scrape <niche> <city> <state>
  python cli.py --help
"""


def print_help():
    print(HELP_TEXT)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h", "help"):
        print_help()
        return

    cmd = sys.argv[1].lower()

    if cmd == "run":
        if len(sys.argv) < 4:
            print("Usage: python cli.py run <name> <project>")
            return
        commands.marko_run(sys.argv[2], sys.argv[3])

    elif cmd == "add_lead":
        if len(sys.argv) < 5:
            print("Usage: python cli.py add_lead <name> <email> <niche>")
            return
        commands.add_lead(sys.argv[2], sys.argv[3], sys.argv[4])

    elif cmd == "send":
        dry_run = "--dry-run" in sys.argv
        commands.marko_send(dry_run=dry_run)

    elif cmd == "log":
        if len(sys.argv) < 3:
            print("Usage: python cli.py log <count> [opens] [replies] [signups]")
            return
        count = int(sys.argv[2])
        opens = int(sys.argv[3]) if len(sys.argv) > 3 else 0
        replies = int(sys.argv[4]) if len(sys.argv) > 4 else 0
        signups = int(sys.argv[5]) if len(sys.argv) > 5 else 0
        commands.marko_log(count, opens, replies, signups)

    elif cmd == "analyze":
        commands.marko_analyze()

    elif cmd == "report":
        commands.marko_report()

    elif cmd == "scrape":
        if len(sys.argv) < 5:
            print("Usage: python cli.py scrape <niche> <city> <state>")
            return
        scraper.scrape(sys.argv[2], sys.argv[3], sys.argv[4])

    else:
        print(f"Unknown: {cmd}")
        print_help()


if __name__ == "__main__":
    main()
