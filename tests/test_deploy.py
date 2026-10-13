import pytest

from app.rag.check_index import validate_index


def test_deploy_requires_complete_current_index(corpus):
    settings, _ = corpus
    directory = validate_index(settings)
    (directory / "extras.npz").unlink()
    with pytest.raises(ValueError, match="Run indexing explicitly"):
        validate_index(settings)


def test_deploy_rejects_changed_data_without_rebuilding(corpus):
    settings, _ = corpus
    directory = validate_index(settings)
    settings.faq_path.write_bytes(b"updated spreadsheet")
    with pytest.raises(ValueError, match="outdated"):
        validate_index(settings)
    assert directory.is_dir()


def test_deploy_rejects_absent_index(tmp_path):
    from app.config import Settings

    settings = Settings(_env_file=None, index_dir=tmp_path / "missing")
    with pytest.raises(ValueError, match="Run indexing explicitly"):
        validate_index(settings)
    assert not settings.index_dir.exists()
