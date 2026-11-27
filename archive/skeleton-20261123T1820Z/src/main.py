"""Main entry point for AI Radar V1.0 research agent."""

import asyncio
import sys
import os
from pathlib import Path

# Add the src directory to path
sys.path.insert(0, str(Path(__file__).parent))

from ai_radar.settings import get_settings, load_yaml_config
from ai_radar.operations import ResearchPipeline


async def run_pipeline():
    """Run a single research pipeline iteration."""
    print("=" * 60)
    print("AI Radar V1.0 - Research Pipeline")
    print("=" * 60)
    print()

    # Load settings and validate configuration
    settings = get_settings()
    print(f"Configuration loaded: {len(settings.get('sources', {}).get('sources', {}))} sources configured")
    print(f"Categories: {len(settings.get('categories', {}).get('categories', {}))}")
    print(f"Scoring weights: {settings.get('scoring', {}).get('weights', {})}")
    print()

    # Initialize and run pipeline
    pipeline = ResearchPipeline()
    results = await pipeline.run_full_pipeline()

    print()
    print("=" * 60)
    print("PIPELINE RESULTS")
    print("=" * 60)
    print(f"  Discoveries collected: {results.get('discoveries_collected', 0)}")
    print(f"  Discoveries after dedup: {results.get('discoveries_deduped', 0)}")
    print(f"  Discoveries verified: {results.get('discoveries_verified', 0)}")
    print(f"  Discoveries persisted: {results.get('discoveries_persisted', 0)}")
    print(f"  Discoveries notified: {results.get('discoveries_notified', 0)}")
    print(f"  New discoveries: {results.get('new_discoveries', 0)}")
    print()
    print("Pipeline complete.")
    print("=" * 60)


def main():
    """CLI entry point."""
    asyncio.run(run_pipeline())


if __name__ == "__main__":
    main()