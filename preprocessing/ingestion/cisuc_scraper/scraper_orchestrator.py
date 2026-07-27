#!/usr/bin/env python3
"""
Orchestrator for the Chatbot Data Scraper.

Coordinates:
- Static website content via web crawler
- API data for users, projects, and publications
- News articles via Selenium crawler
"""

import argparse
from pathlib import Path
from typing import Any

import yaml

from .data_sources.api_data import APIDataSource
from .data_sources.news_data import NewsDataSource
from .data_sources.static_content import StaticContentSource
from .extractors.utils import Logger


DEFAULT_CONFIG_FILE = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "scraper.yaml"
)


class OrchestratorConfig:
    """Load and expose the scraper YAML configuration."""

    def __init__(
        self,
        config_file: str | Path = DEFAULT_CONFIG_FILE,
    ) -> None:
        self.config_file = Path(config_file)
        self.config: dict[str, Any] = {}
        self._load_config()

    def _load_config(self) -> None:
        """Load configuration from the YAML file."""

        if not self.config_file.exists():
            Logger.log_error(
                f"Configuration file not found: {self.config_file}"
            )
            return

        try:
            with self.config_file.open("r", encoding="utf-8") as file:
                self.config = yaml.safe_load(file) or {}

            Logger.log_success(
                f"Configuration loaded from {self.config_file}"
            )

        except yaml.YAMLError as exc:
            Logger.log_error(
                f"Failed to parse YAML configuration: {exc}"
            )

        except OSError as exc:
            Logger.log_error(
                f"Error loading configuration: {exc}"
            )

    def get(self, key: str, default: Any = None) -> Any:
        """Return a top-level configuration value."""

        return self.config.get(key, default)

    def is_source_enabled(self, source: str) -> bool:
        """Return whether a source is enabled."""

        sources = self.config.get("sources", {})
        return sources.get(source, {}).get("enabled", False)


class Orchestrator:
    """Coordinate ingestion across the available data sources."""

    def __init__(self, config: OrchestratorConfig) -> None:
        self.config = config
        self.sources: dict[str, Any] = {}
        self.results: dict[str, bool] = {}

        self._initialize_sources()

    def _initialize_sources(self) -> None:
        """Initialize all implemented data sources."""

        Logger.log_info("Initializing data sources...")

        self.sources["static"] = StaticContentSource(
            self.config.config
        )

        self.sources["api"] = APIDataSource(
            self.config.config
        )

        self.sources["news"] = NewsDataSource(
            self.config.config
        )

        Logger.log_success("Data sources initialized")

    def run(
        self,
        sources_to_run: list[str] | None = None,
    ) -> bool:
        """
        Run selected sources or all enabled sources.

        Args:
            sources_to_run:
                Source names to execute. When omitted, all enabled and
                implemented sources are executed.

        Returns:
            True when every attempted source succeeds.
        """

        Logger.initialize()
        Logger.log_info("=" * 80)
        Logger.log_info("CHATBOT SCRAPER ORCHESTRATOR STARTED")
        Logger.log_info("=" * 80)

        if sources_to_run is None:
            sources_to_run = [
                source_name
                for source_name, source in self.sources.items()
                if getattr(source, "enabled", False)
            ]

        if not sources_to_run:
            Logger.log_warning(
                "No data sources enabled or specified"
            )
            return False

        Logger.log_info(
            f"Running data sources: {', '.join(sources_to_run)}"
        )

        all_success = True

        for source_name in sources_to_run:
            source = self.sources.get(source_name)

            if source is None:
                Logger.log_error(
                    f"Unknown data source: {source_name}"
                )
                self.results[source_name] = False
                all_success = False
                continue

            Logger.log_info("")
            Logger.log_info("=" * 80)
            Logger.log_info(
                f"Running {source_name.upper()} data source"
            )
            Logger.log_info("=" * 80)

            try:
                if not source.initialize():
                    Logger.log_error(
                        f"Failed to initialize {source_name} source"
                    )
                    self.results[source_name] = False
                    all_success = False
                    continue

                success = source.fetch()
                self.results[source_name] = success

                if not success:
                    all_success = False

                if hasattr(source, "get_report"):
                    try:
                        report = source.get_report()

                        if report:
                            Logger.log_info(
                                f"Report saved: {report}"
                            )

                    except Exception as exc:
                        Logger.log_warning(
                            f"Could not generate report for "
                            f"{source_name}: {exc}"
                        )

            except Exception as exc:
                Logger.log_error(
                    f"Error running {source_name} source: {exc}"
                )
                self.results[source_name] = False
                all_success = False

            finally:
                if hasattr(source, "close"):
                    try:
                        source.close()

                    except Exception as exc:
                        Logger.log_warning(
                            f"Error closing {source_name}: {exc}"
                        )

        self._print_summary()

        Logger.log_info("")
        Logger.log_info("=" * 80)
        Logger.log_info("ORCHESTRATOR COMPLETED")
        Logger.log_info("=" * 80)

        return all_success

    def _print_summary(self) -> None:
        """Print the execution results."""

        Logger.log_info("")
        Logger.log_info("=" * 80)
        Logger.log_info("EXECUTION SUMMARY")
        Logger.log_info("=" * 80)

        if not self.results:
            Logger.log_warning("No sources were executed")
            return

        for source, success in self.results.items():
            status = "SUCCESS" if success else "FAILED"
            icon = "[✓]" if success else "[✗]"

            Logger.log_info(
                f"{icon} {source.upper()}: {status}"
            )

        overall_success = all(self.results.values())
        overall_status = (
            "SUCCESS"
            if overall_success
            else "PARTIAL/FAILED"
        )

        Logger.log_info(
            f"OVERALL: {overall_status}"
        )


def main() -> None:
    """CLI entry point."""

    parser = argparse.ArgumentParser(
        description="Orchestrator for Chatbot Data Scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m preprocessing.ingestion.cisuc_scraper.scraper_orchestrator
  python -m preprocessing.ingestion.cisuc_scraper.scraper_orchestrator --source static
  python -m preprocessing.ingestion.cisuc_scraper.scraper_orchestrator --source api
  python -m preprocessing.ingestion.cisuc_scraper.scraper_orchestrator --source news
  python -m preprocessing.ingestion.cisuc_scraper.scraper_orchestrator --source static api
        """,
    )

    parser.add_argument(
        "--source",
        nargs="+",
        choices=["static", "api", "news"],
        help=(
            "Specific source or sources to run. "
            "When omitted, all enabled sources are run."
        ),
    )

    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_FILE,
        help=(
            "Path to the scraper configuration file. "
            f"Default: {DEFAULT_CONFIG_FILE}"
        ),
    )

    args = parser.parse_args()

    config = OrchestratorConfig(args.config)

    if not config.config:
        Logger.log_error(
            "Cannot start ingestion without a valid "
            f"configuration file: {args.config}"
        )
        raise SystemExit(2)

    orchestrator = Orchestrator(config)

    success = orchestrator.run(
        sources_to_run=args.source
    )

    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()