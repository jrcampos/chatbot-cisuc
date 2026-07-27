"""
Chatbot Scraper Package
Orchestrates data collection from multiple sources for the chatbot project
"""

from .scraper_orchestrator import Orchestrator, OrchestratorConfig

__version__ = "1.0.0"
__all__ = ["Orchestrator", "OrchestratorConfig"]
