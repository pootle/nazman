import pytest
from nazman.utils.validation import (
    validate_pool_name,
    validate_dataset_name,
    validate_device_path,
    validate_ip_cidr,
    validate_size_string,
    validate_schedule,
)
from nazman.utils.exceptions import ValidationError


class TestValidatePoolName:
    def test_valid_names(self):
        assert validate_pool_name("mypool") == "mypool"
        assert validate_pool_name("pool-1") == "pool-1"
        assert validate_pool_name("pool_2.test") == "pool_2.test"
        assert validate_pool_name("UPPERCASE") == "uppercase"

    def test_empty_name(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_pool_name("")

    def test_too_long(self):
        with pytest.raises(ValidationError, match="too long"):
            validate_pool_name("a" * 257)

    def test_invalid_characters(self):
        with pytest.raises(ValidationError, match="only contain"):
            validate_pool_name("pool name with spaces")

        with pytest.raises(ValidationError, match="only contain"):
            validate_pool_name("pool/slash")

    def test_returns_lowercase(self):
        assert validate_pool_name("MYPOOL") == "mypool"


class TestValidateDatasetName:
    def test_valid_names(self):
        assert validate_dataset_name("data") == "data"
        assert validate_dataset_name("pool/dataset") == "pool/dataset"
        assert validate_dataset_name("pool/data/sub") == "pool/data/sub"

    def test_empty_name(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_dataset_name("")

    def test_too_long(self):
        with pytest.raises(ValidationError, match="too long"):
            validate_dataset_name("a" * 257)

    def test_invalid_characters(self):
        with pytest.raises(ValidationError, match="only contain"):
            validate_dataset_name("data name")

    def test_returns_lowercase(self):
        assert validate_dataset_name("Data/Sub") == "data/sub"


class TestValidateDevicePath:
    def test_valid_path(self):
        assert validate_device_path("/dev/sda") == "/dev/sda"
        assert validate_device_path("/dev/nvme0n1") == "/dev/nvme0n1"

    def test_empty_path(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_device_path("")

    def test_missing_dev_prefix(self):
        with pytest.raises(ValidationError, match="must start with /dev/"):
            validate_device_path("sda")


class TestValidateIpCidr:
    def test_valid_ip(self):
        assert validate_ip_cidr("192.168.1.1") == "192.168.1.1"

    def test_valid_cidr(self):
        assert validate_ip_cidr("192.168.1.0/24") == "192.168.1.0/24"

    def test_empty_cidr(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_ip_cidr("")

    def test_invalid_format(self):
        with pytest.raises(ValidationError, match="Invalid IP/CIDR"):
            validate_ip_cidr("not-an-ip")

    def test_invalid_octet(self):
        with pytest.raises(ValidationError, match="Invalid IP"):
            validate_ip_cidr("192.168.1.999")

    def test_invalid_prefix(self):
        with pytest.raises(ValidationError, match="Invalid CIDR prefix"):
            validate_ip_cidr("192.168.1.0/33")


class TestValidateSizeString:
    def test_valid_sizes(self):
        assert validate_size_string("10G") == "10g"
        assert validate_size_string("1T") == "1t"
        assert validate_size_string("500M") == "500m"
        assert validate_size_string("1024K") == "1024k"
        assert validate_size_string("1P") == "1p"
        assert validate_size_string("100") == "100"

    def test_empty_size(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_size_string("")

    def test_invalid_format(self):
        with pytest.raises(ValidationError, match="Invalid size"):
            validate_size_string("10GB")


class TestValidateSchedule:
    def test_valid_schedules(self):
        assert validate_schedule("0 2 * * 0") == "0 2 * * 0"
        assert validate_schedule("*/5 * * * *") == "*/5 * * * *"
        assert validate_schedule("30 1 1,15 * *") == "30 1 1,15 * *"

    def test_empty_schedule(self):
        with pytest.raises(ValidationError, match="cannot be empty"):
            validate_schedule("")

    def test_wrong_field_count(self):
        with pytest.raises(ValidationError, match="5 fields"):
            validate_schedule("0 2 * *")

    def test_invalid_field(self):
        with pytest.raises(ValidationError, match="Invalid schedule field"):
            validate_schedule("0 2 * * abc")
