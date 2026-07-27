"""
API Retriever

This module provides the APIRetriever class, which handles authenticated HTTP 
requests to CISUC API endpoints, including paginated data retrieval and 
local storage of JSON artifacts.
"""

import requests
import json
import os
from typing import Any
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class APIRetriever:
    """
    Low-level retriever for interacting with CISUC API endpoints.
    
    Handles authentication, request headers, pagination, and response consolidation.
    """
    
    def __init__(self, token: str, base_url: str = "https://www.cisuc.uc.pt") -> None:
        """
        Initialize the retriever with authentication credentials.
        
        Args:
            token: Bearer token for API authorization.
            base_url: The root URL for the API service.
        """
        self.token = token
        self.base_url = base_url
        self.endpoints: list[str] = ["api-users", "api-projects", "api-publications"]
    
    def set_endpoints(self, endpoints: list[str]) -> None:
        """
        Override the default set of endpoints to be retrieved.
        
        Args:
            endpoints: A list of target endpoint strings.
        """
        self.endpoints = endpoints
        logger.info(f"Custom endpoints set: {endpoints}")
    
    def _prepare_headers(self) -> dict[str, str]:
        """
        Construct the standard authorization and content-type headers.
        
        Returns:
            dict[str, str]: A dictionary of HTTP headers.
        """
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
    
    def fetch_endpoint(self, endpoint: str, timeout: int = 30) -> dict[str, Any] | None:
        """
        Retrieve all data from a specific API endpoint, traversing all available pages.
        
        Args:
            endpoint: The specific endpoint path (e.g., 'api-users').
            timeout: Request timeout in seconds.
            
        Returns:
            dict[str, Any] | None: A consolidated dictionary containing all retrieved items, 
                                   or None if the initial request failed.
        """
        try:
            url = f"{self.base_url}/{endpoint}"
            headers = self._prepare_headers()
            payload: dict[str, Any] = {}
            params: dict[str, Any] = {
                "page": 1,
                "per_page": 100
            }
            responses: dict[str, Any] = {"data": []}

            logger.info(f"Fetching from {endpoint}...")
            
            response = requests.post(
                url,
                headers=headers,
                json=payload,
                params=params,
                timeout=timeout
            )
            
            if response.status_code == 200:
                logger.info(f"Successfully fetched {endpoint}")

                response_json = response.json()
                last_page = response_json.get("meta", {}).get("last_page", 1)
                
                logger.info(f"{endpoint} - Total pages: {last_page}")
                responses["data"].extend(response_json.get("data", []))
            else:
                logger.error(
                    f"Failed to fetch {endpoint}. Status code: {response.status_code}"
                )
                return None
            
            # Traverse remaining pages
            while params["page"] < last_page:
                params["page"] += 1
                
                response = requests.post(
                    url,
                    headers=headers,
                    json=payload,
                    params=params,
                    timeout=timeout
                )
                
                if response.status_code == 200:
                    responses["data"].extend(response.json().get("data", []))
                else:
                    logger.error(
                        f"Failed to fetch page {params['page']} of {endpoint}. Status code: {response.status_code}"
                    )
                    break
            
            return responses    
        
        except requests.Timeout:
            logger.error(f"Timeout while fetching {endpoint}")
            return None
        except requests.RequestException as e:
            logger.error(f"Request error fetching {endpoint}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error fetching {endpoint}: {e}")
            return None
    
    def fetch_all_endpoints(self, timeout: int = 30) -> dict[str, dict[str, Any] | None]:
        """
        Sequentially fetch data from all configured endpoints.
        
        Args:
            timeout: Request timeout in seconds per endpoint.
            
        Returns:
            dict[str, dict[str, Any] | None]: A dictionary mapping endpoint names 
                                              to their resulting data.
        """
        results: dict[str, dict[str, Any] | None] = {}
        
        for endpoint in self.endpoints:
            data = self.fetch_endpoint(endpoint, timeout)
            results[endpoint] = data
        
        return results
    
    def save_endpoint_data(self, endpoint: str, data: dict[str, Any], output_dir: str) -> bool:
        """
        Serialize endpoint data into a local JSON file.
        
        Args:
            endpoint: The endpoint name, used as the filename.
            data: The dictionary of data to save.
            output_dir: Target directory for the file.
            
        Returns:
            bool: True if serialization was successful, False otherwise.
        """
        try:
            os.makedirs(output_dir, exist_ok=True)
            
            output_file = os.path.join(output_dir, f"{endpoint}.json")
            
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            logger.info(f"Saved API response to {output_file}")
            return True
        
        except Exception as e:
            logger.error(f"Error saving {endpoint} data: {e}")
            return False
    
    def fetch_and_save_all(self, output_dir: str, timeout: int = 30) -> dict[str, bool]:
        """
        Execute the full pipeline of fetching and saving for all configured endpoints.
        
        Args:
            output_dir: Target directory for storage.
            timeout: Request timeout per endpoint.
            
        Returns:
            dict[str, bool]: A dictionary mapping endpoint names to their success status.
        """
        logger.info(f"Fetching and saving all {len(self.endpoints)} endpoints...")
        
        results: dict[str, bool] = {}
        fetched_data = self.fetch_all_endpoints(timeout)
        
        for endpoint, data in fetched_data.items():
            if data is not None:
                success = self.save_endpoint_data(endpoint, data, output_dir)
                results[endpoint] = success
            else:
                results[endpoint] = False
        
        success_count = sum(1 for v in results.values() if v)
        logger.info(f"Completed: {success_count}/{len(self.endpoints)} endpoints saved successfully")
        
        return results


if __name__ == "__main__":
    # Example standalone usage
    import os
    
    token = os.getenv("CISUC_TOKEN")
    
    if not token:
        print("Error: CISUC_TOKEN environment variable not set")
        exit(1)
    
    retriever = APIRetriever(token)
    output_dir = os.path.join(os.getcwd(), 'data', 'api')
    
    results = retriever.fetch_and_save_all(output_dir)
    
    for endpoint, success in results.items():
        status = "✓" if success else "✗"
        print(f"{status} {endpoint}")
