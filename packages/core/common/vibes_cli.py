"""Fun CLI tool for checking the vibes of your influencer data.

Usage:
    uv run python -m common.vibes_cli [--api-url URL]

Shows creative, entertaining insights about your influencer data.
"""

import argparse
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json


def fetch_vibes(api_url: str) -> dict:
    """Fetch vibes from the API."""
    req = Request(f"{api_url}/vibes")
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        print(f"API error: {e.code} {e.reason}", file=sys.stderr)
        sys.exit(1)
    except URLError as e:
        print(f"Connection error: {e.reason}", file=sys.stderr)
        sys.exit(1)


def display_vibes(vibes: dict) -> None:
    """Display vibes in a fun, colorful way."""
    print("\n" + "=" * 60)
    print("✨ INFLUENCER VIBES CHECK ✨".center(60))
    print("=" * 60 + "\n")
    
    print(f"📊 Total Signals: {vibes['total_signals']:,}")
    print(f"👥 Total Influencers: {vibes['total_influencers']}")
    print(f"⚡ Energy Level: {vibes['energy_level'].upper()}")
    print(f"💭 Fun Fact: {vibes['fun_fact']}")
    
    print("\n" + "-" * 60)
    print("🎯 VIBE CHECK")
    print("-" * 60)
    print(f"   {vibes['vibe_check']}")
    
    if vibes.get('most_active_influencer'):
        ma = vibes['most_active_influencer']
        print("\n" + "-" * 60)
        print("🏆 MOST ACTIVE INFLUENCER")
        print("-" * 60)
        print(f"   @{ma['handle']} ({ma['name']})")
        print(f"   {ma['signal_count']:,} signals")
        print(f"   {ma['vibe']}")
    
    print("\n" + "=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Check the vibes of your influencer data"
    )
    parser.add_argument(
        "--api-url",
        default="http://localhost:8000",
        help="API base URL (default: http://localhost:8000)"
    )
    
    args = parser.parse_args()
    
    print("Fetching vibes...", end=" ", flush=True)
    vibes = fetch_vibes(args.api_url)
    print("✓")
    
    display_vibes(vibes)


if __name__ == "__main__":
    main()
