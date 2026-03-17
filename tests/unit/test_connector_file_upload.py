import pytest

from aidomaincontext.connectors.file_upload import FileUploadConnector


@pytest.fixture
def connector():
    return FileUploadConnector()


def test_connector_type(connector):
    assert connector.connector_type == "file_upload"


@pytest.mark.asyncio
async def test_validate_credentials(connector):
    assert await connector.validate_credentials({}) is True


def test_create_document(connector):
    doc = connector.create_document("test.txt", "Hello world content")
    assert doc.title == "test.txt"
    assert doc.content == "Hello world content"
    assert doc.source_type == "file_upload"
    assert doc.source_id.startswith("upload:")
    assert doc.metadata == {"filename": "test.txt"}


def test_create_document_unique_ids(connector):
    doc1 = connector.create_document("test.txt", "content1")
    doc2 = connector.create_document("test.txt", "content2")
    # Same filename should produce different source_ids (uuid component)
    assert doc1.source_id != doc2.source_id
