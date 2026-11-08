"""Exercise the HTTP handler against the real catalogue, not an unrestricted mock."""
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter


@pytest.mark.asyncio
async def test_skills_http_reads_versioned_external_catalog(tmp_path, monkeypatch):
    from tools import skills_tool
    local = tmp_path / 'local'
    shared = tmp_path / 'shared'
    local.mkdir()
    skill = shared / 'customer-core' / 'SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('---\nname: customer-core\ndescription: Customer guidance\nversion: 1.0.0\n---\nOperational guidance.\n')
    monkeypatch.setattr(skills_tool, '_skills_dir', lambda: local)
    from agent import skill_utils
    monkeypatch.setattr(skill_utils, 'get_skill_search_roots',
                        lambda active: [(skill_utils.TIER_LOCAL, local),
                                        (skill_utils.TIER_EXTERNAL, shared)])
    skills_tool._SKILLS_CACHE.clear()
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={}))
    app = web.Application()
    app.router.add_get('/v1/skills', adapter._handle_skills)
    async with TestClient(TestServer(app)) as client:
        response = await client.get('/v1/skills')
        assert response.status == 200
        result = await response.json()
    entry = next(row for row in result['data'] if row['name'] == 'customer-core')
    assert entry['version'] == '1.0.0'
    assert result['object'] == 'list'
    assert 'path' not in entry  # do not expose server filesystem paths
