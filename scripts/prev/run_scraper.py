#!/usr/bin/env python3
"""
Entry point for running the orchestrator
Use: python run_scraper.py [--source static|api|news] [--config config.yaml]
"""

import importlib


main = importlib.import_module(
    "1_ingestion.cisuc_scraper.scraper_orchestrator"
).main

if __name__ == '__main__':
    main()
