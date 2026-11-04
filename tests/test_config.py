"""Settings loaded from the environment and `.env`."""

from __future__ import annotations

import os

from oxylabs_web_api_mcp import config


def test_env_file_fills_unset_oxylabs_vars_and_nothing_else():
    import tempfile as _tempfile

    with _tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, ".env")
        with open(path, "w") as fh:
            fh.write(
                "# a comment\n"
                "\n"
                'export OXYLABS_WEB_API_KEY="from-file"\n'
                "OXYLABS_SPILL_DIR=/from/file\n"
                "PATH=/definitely/not\n"
            )

        saved_path, saved_dir = os.environ["PATH"], os.environ.pop("OXYLABS_SPILL_DIR", None)
        os.environ["OXYLABS_ENV_FILE"] = path
        try:
            config._load_env_file()
            # The real environment wins: the key is already set, so the file must not win.
            assert os.environ["OXYLABS_WEB_API_KEY"] == "test-key"
            assert os.environ["OXYLABS_SPILL_DIR"] == "/from/file"
            assert os.environ["PATH"] == saved_path, "only OXYLABS_* may come from a .env"

            # An MCP config interpolating ${OXYLABS_WEB_API_KEY} passes "" when it is
            # unset; that must not shadow the file.
            saved_key = os.environ["OXYLABS_WEB_API_KEY"]
            os.environ["OXYLABS_WEB_API_KEY"] = ""
            try:
                config._load_env_file()
                assert os.environ["OXYLABS_WEB_API_KEY"] == "from-file"
            finally:
                os.environ["OXYLABS_WEB_API_KEY"] = saved_key
        finally:
            os.environ.pop("OXYLABS_ENV_FILE", None)
            os.environ.pop("OXYLABS_SPILL_DIR", None)
            if saved_dir is not None:
                os.environ["OXYLABS_SPILL_DIR"] = saved_dir
