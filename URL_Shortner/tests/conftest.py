import os
import pathlib

os.environ["DATABASE_URL"] = "sqlite:///./test.db"  # real DB ko haath nahi lagayenge
pathlib.Path("test.db").unlink(missing_ok=True)

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def client():
    main.rate_hits.clear()
    main.link_cache.clear()
    with TestClient(main.app) as c:  # "with" se click worker thread bhi start hota hai
        yield c