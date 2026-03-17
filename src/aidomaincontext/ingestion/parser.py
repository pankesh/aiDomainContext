import structlog
from unstructured.partition.auto import partition

logger = structlog.get_logger()


def extract_text(file_path: str | None = None, raw_text: str | None = None) -> str:
    """Extract text from a file using unstructured, or pass through raw text."""
    if raw_text is not None:
        return raw_text

    if file_path is None:
        raise ValueError("Either file_path or raw_text must be provided")

    logger.info("parsing_file", path=file_path)
    elements = partition(filename=file_path)
    return "\n\n".join(str(el) for el in elements)
