"""
API Data Source

This module implements the APIDataSource class, which coordinates the retrieval 
of data from various CISUC API endpoints (e.g., Users, Projects, Publications).
It utilizes an APIRetriever and manages the resulting data artifacts via a FormatManager.
"""

import os
from pathlib import Path
from typing import Any

from ..extractors.utils import Logger
from ..crawlers.utils.api_retriever import APIRetriever
from ..converters import FormatManager

from preprocessing.paths import resolve_raw_path

class APIDataSource:
    """
    Data source for fetching and processing information from CISUC API endpoints.
    
    Coordinates authentication, endpoint configuration, and subsequent format conversion.
    """
    
    def __init__(self, config: dict[str, Any]) -> None:
        """
        Initialize the API data source with configuration parameters.
        
        Args:
            config: A dictionary containing system-wide configuration settings.
        """
        self.config: dict[str, Any] = config.get('api', {}) 
        self.enabled: bool = config.get('sources', {}).get('api', {}).get('enabled', True)
        self.output_dir = resolve_raw_path(
            config.get("sources", {})
            .get("api", {})
            .get("output_dir", "api")
        )
        self.endpoints: list[str] = config.get('sources', {}).get('api', {}).get('endpoints', [
            'api-users',
            'api-projects',
            'api-publications'
        ])
        
        # Output format configuration
        output_format = config.get('output', {}).get('format', 'json')
        self.format_manager = FormatManager(output_format=output_format)
        
        self.base_url = "https://www.cisuc.uc.pt"
        self.token = os.getenv("CISUC_TOKEN")
        self.timeout: int = self.config.get('timeout', 30)
        self.retriever: APIRetriever | None = None
    
    def initialize(self) -> bool:
        """
        Set up the API retriever and ensure the environment is ready for fetching.
        
        Returns:
            bool: True if initialization was successful, False otherwise.
        """
        Logger.log_info("Initializing API Data Source...")
        
        if not self.token:
            Logger.log_warning("CISUC_TOKEN environment variable not set")
            return False
        
        # Create output directory
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        
        try:
            # Initialize the API retriever
            Logger.log_info(f"API base URL: {self.base_url}")
            Logger.log_info(
                f"API endpoints: {', '.join(self.endpoints)}"
            )
            self.retriever = APIRetriever(self.token, self.base_url)
            self.retriever.set_endpoints(self.endpoints)
            Logger.log_success("API Data Source initialized")
            return True
        except Exception as e:
            Logger.log_error(f"Error initializing API retriever: {e}")
            return False

    @staticmethod
    def _should_keep_user(
            user: dict[str, Any],
            target_categories: set[str],
    ) -> bool:
        category = user.get("category")
        return category in target_categories

    def _filter_users(
            self,
            payload: dict[str, Any],
    ) -> dict[str, Any]:
        users = payload.get("data", [])

        if not isinstance(users, list):
            raise ValueError(
                "Unexpected api-users response: 'data' must be a list"
            )

        target_categories = set(
            self.config.get("target_user_categories", [])
        )

        if not target_categories:
            Logger.log_info(
                "No target user categories configured; keeping all users"
            )
            return payload

        filtered_users = [
            user
            for user in users
            if self._should_keep_user(user, target_categories)
        ]

        filtered_payload = dict(payload)
        filtered_payload["data"] = filtered_users

        Logger.log_info(
            f"Filtered API users by target category: "
            f"{len(users)} -> {len(filtered_users)}"
        )

        return filtered_payload
    
    def fetch(self) -> bool:
        """
        Fetch data from all configured API endpoints and trigger format conversion.
        
        Returns:
            bool: True if at least one endpoint was fetched successfully, False otherwise.
        """
        if not self.enabled:
            Logger.log_info("API data source is disabled")
            return True
        
        Logger.log_info("Fetching API data...")
        
        if not self.token:
            Logger.log_error("CISUC_TOKEN not available")
            return False
        
        if not self.retriever:
            Logger.log_error("API retriever not initialized")
            return False
        
        try:
            # Fetch and save all endpoints
            fetched_data = self.retriever.fetch_all_endpoints(self.timeout)

            results: dict[str, bool] = {}

            for endpoint, data in fetched_data.items():
                if data is None:
                    results[endpoint] = False
                    continue

                if endpoint == "api-users":
                    data = self._filter_users(data)

                results[endpoint] = self.retriever.save_endpoint_data(
                    endpoint,
                    data,
                    self.output_dir,
                )
                
            success_count = sum(1 for v in results.values() if v)
            
            if success_count == 0:
                Logger.log_error("Failed to fetch any API endpoints")
                return False
            elif success_count < len(self.endpoints):
                Logger.log_warning(f"Partially successful: {success_count}/{len(self.endpoints)} endpoints")
            else:
                Logger.log_success(f"All {success_count} API endpoints fetched successfully")
            
            # Convert format if needed
            Logger.log_info(f"Processing API format: {self.format_manager.format.value}")
            conversion_result = self.format_manager.convert_api_data(
                api_json_dir=self.output_dir,
                output_base_dir=self.output_dir
            )
            
            if not conversion_result["success"]:
                Logger.log_warning(f"Format conversion warnings: {conversion_result['errors']}")
            else:
                Logger.log_success("API format conversion completed")
            
            return True
        
        except Exception as e:
            Logger.log_error(f"Error fetching API data: {e}")
            return False

