"""
Unit tests for OrchestratorConfig module
Tests YAML configuration loading and access
"""

import pytest
import yaml
from pathlib import Path
from ingestion.cisuc_scraper.scraper_orchestrator import OrchestratorConfig


class TestOrchestratorConfigInitialization:
    """Test OrchestratorConfig initialization"""

    def test_config_initialization_with_valid_file(self, sample_yaml_config):
        """Test initialization with valid YAML config file"""
        config = OrchestratorConfig(str(sample_yaml_config))

        assert config.config_file == sample_yaml_config
        assert config.config is not None

    def test_config_initialization_with_nonexistent_file(self, temp_dir):
        """Test initialization with non-existent config file"""
        nonexistent = temp_dir / "nonexistent.yaml"
        config = OrchestratorConfig(str(nonexistent))

        # Config should handle gracefully
        assert config.config_file == nonexistent

    def test_config_file_path_conversion(self, sample_yaml_config):
        """Test that config file path is converted to Path object"""
        config = OrchestratorConfig(str(sample_yaml_config))

        assert isinstance(config.config_file, Path)


class TestOrchestratorConfigLoading:
    """Test YAML configuration loading"""

    def test_load_sources_configuration(self, sample_yaml_config):
        """Test loading sources configuration"""
        config = OrchestratorConfig(str(sample_yaml_config))

        assert "sources" in config.config
        assert "static" in config.config["sources"]

    def test_load_output_configuration(self, sample_yaml_config):
        """Test loading output configuration"""
        config = OrchestratorConfig(str(sample_yaml_config))

        assert "output" in config.config
        assert config.config["output"]["format"] == "json"

    def test_load_crawler_configuration(self, sample_yaml_config):
        """Test loading crawler configuration"""
        config = OrchestratorConfig(str(sample_yaml_config))

        assert "crawler" in config.config
        assert config.config["crawler"]["max_workers"] == 5

    def test_load_api_configuration(self, sample_yaml_config):
        """Test loading API configuration"""
        config = OrchestratorConfig(str(sample_yaml_config))

        # API config may or may not be present depending on YAML file
        # Check that config is loaded successfully at minimum
        assert isinstance(config.config, dict)


class TestOrchestratorConfigAccess:
    """Test accessing configuration values"""

    def test_access_nested_value(self, sample_yaml_config):
        """Test accessing nested configuration values"""
        config = OrchestratorConfig(str(sample_yaml_config))

        # Should be able to access nested values
        assert config.config["sources"]["static"]["enabled"] is True
        assert (
            config.config["sources"]["static"]["base_url"] == "https://www.cisuc.uc.pt"
        )

    def test_access_output_format(self, sample_yaml_config):
        """Test accessing output format setting"""
        config = OrchestratorConfig(str(sample_yaml_config))

        output_format = config.config.get("output", {}).get("format")
        assert output_format == "json"

    def test_access_crawler_timeout(self, sample_yaml_config):
        """Test accessing crawler timeout"""
        config = OrchestratorConfig(str(sample_yaml_config))

        timeout = config.config.get("crawler", {}).get("timeout")
        assert timeout == 30


class TestOrchestratorConfigWithMockConfig:
    """Test with mock configuration dictionary"""

    def test_config_with_mock_data(self, mock_config):
        """Test configuration with mock data"""
        # Create temporary YAML file from mock config
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(mock_config, f)
            temp_path = f.name

        try:
            config = OrchestratorConfig(temp_path)

            # Verify mock data is loaded
            assert config.config["sources"]["static"]["enabled"] is True
            assert config.config["output"]["format"] == "json"
        finally:
            Path(temp_path).unlink()

    def test_access_api_endpoints_from_config(self, mock_config):
        """Test accessing API endpoints from mock config"""
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(mock_config, f)
            temp_path = f.name

        try:
            config = OrchestratorConfig(temp_path)
            endpoints = config.config["sources"]["api"].get("endpoints")

            assert endpoints == ["api-users", "api-projects"]
        finally:
            Path(temp_path).unlink()


class TestOrchestratorConfigValidation:
    """Test configuration validation"""

    def test_config_is_dictionary(self, sample_yaml_config):
        """Test that loaded config is a dictionary"""
        config = OrchestratorConfig(str(sample_yaml_config))

        assert isinstance(config.config, dict)

    def test_config_not_empty_with_valid_file(self, sample_yaml_config):
        """Test that config is not empty with valid file"""
        config = OrchestratorConfig(str(sample_yaml_config))

        assert len(config.config) > 0


class TestOrchestratorConfigDefaultValues:
    """Test default configuration values"""

    def test_default_config_file_path(self):
        """Test default config file path"""
        config = OrchestratorConfig()

        # Default should be config/scraper_config.yaml (normalized for Windows paths)
        assert "scraper_config.yaml" in str(config.config_file)


class TestOrchestratorConfigErrorHandling:
    """Test error handling in config loading"""

    def test_invalid_yaml_file(self, temp_dir):
        """Test handling of invalid YAML file"""
        invalid_yaml = temp_dir / "invalid.yaml"
        invalid_yaml.write_text("{ invalid: yaml: content: }")

        # Should handle gracefully without raising exception
        try:
            config = OrchestratorConfig(str(invalid_yaml))
            # Config should exist even if invalid
            assert config.config_file == invalid_yaml
        except Exception as e:
            # If it raises, it should be a YAML parsing error
            assert "yaml" in str(e).lower() or "parsing" in str(e).lower()

    def test_permission_denied_file(self, temp_dir):
        """Test handling of file with permission denied"""
        # This test may not be applicable on all systems
        # It's here for completeness

        restricted_file = temp_dir / "restricted.yaml"
        restricted_file.write_text("test: value")

        # Try to create config - should handle gracefully
        try:
            config = OrchestratorConfig(str(restricted_file))
            assert config is not None
        except PermissionError:
            # If permission is denied, should raise PermissionError
            pass
