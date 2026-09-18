"""
Test matrix: Memory
  "Required Qdrant index failure prevents ready state and later
  recovers."

Directly exercises MemoryService._ensure_collection() - the P1-15 fix.
Builds an instance without running __init__ (avoids constructing a real
QdrantClient) and swaps in a mocked client whose create_payload_index
can be made to fail or succeed on demand.
"""
from unittest.mock import MagicMock

from app.services.memory_service import MemoryService
from app.services.dependency_health import dependency_health


def _service_with_mock_client():
    service = MemoryService.__new__(MemoryService)
    service.collection_name = "test_collection"
    service.dimension = 384
    service._ready = False
    service.client = MagicMock()
    service.client.collection_exists = MagicMock(return_value=True)
    return service


def test_ready_stays_false_when_index_creation_fails():
    service = _service_with_mock_client()
    service.client.create_payload_index = MagicMock(side_effect=Exception("index creation failed: permission denied"))

    service._ensure_collection()

    assert service._ready is False
    assert dependency_health.get_status("qdrant") == "down"


def test_ready_becomes_true_once_indexes_succeed():
    service = _service_with_mock_client()
    service.client.create_payload_index = MagicMock()  # succeeds

    service._ensure_collection()

    assert service._ready is True
    assert dependency_health.get_status("qdrant") == "up"


def test_recovers_after_a_prior_failure():
    """Same instance, failed once, then the index call starts succeeding - _ensure_collection must re-check, not stay stuck."""
    service = _service_with_mock_client()
    service.client.create_payload_index = MagicMock(side_effect=Exception("temporary outage"))
    service._ensure_collection()
    assert service._ready is False

    service.client.create_payload_index = MagicMock()  # recovered
    service._ensure_collection()

    assert service._ready is True
    assert dependency_health.get_status("qdrant") == "up"


def test_already_exists_error_counts_as_success_not_failure():
    """Idempotent re-creation: 'already exists' from Qdrant is a success path, not a real failure."""
    service = _service_with_mock_client()
    service.client.create_payload_index = MagicMock(
        side_effect=Exception("Index already exists for field user_id")
    )

    service._ensure_collection()

    assert service._ready is True


def test_ready_stays_false_when_collection_listing_fails():
    service = MemoryService.__new__(MemoryService)
    service.collection_name = "test_collection"
    service.dimension = 384
    service._ready = False
    service.client = MagicMock()
    service.client.collection_exists = MagicMock(side_effect=Exception("cannot reach qdrant"))

    service._ensure_collection()

    assert service._ready is False
    assert dependency_health.get_status("qdrant") == "down"