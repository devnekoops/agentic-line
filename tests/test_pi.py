import asyncio
import sys

import pytest

from kanban.config import Config
from kanban.pi import PiClient


@pytest.fixture
def rpc_client(tmp_path):
    executable = tmp_path / "pi-stub"
    executable.write_text(
        f"#!{sys.executable}\n"
        + """
import json, sys, time
from pathlib import Path
def send(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    kind = request['type']
    with Path('commands.txt').open('a') as log:
        log.write(kind + '\\n')
    data = {}
    if kind == 'get_last_assistant_text': data = {'text': 'finished after retry'}
    if kind == 'get_messages': data = {'messages': []}
    send({'type':'response', 'id':request['id'], 'success':True, 'data':data})
    if kind == 'prompt' and request['message'] != 'hang':
        send({'type':'agent_end'})
        send({'type':'auto_retry_start'})
        time.sleep(.05)
        send({'type':'auto_retry_end', 'success':True})
        send({'type':'agent_settled'})
"""
    )
    executable.chmod(0o700)
    config = Config(tmp_path / "data", pi_bin=str(executable))
    config.prepare()
    return PiClient(config, "test")


async def test_rpc_waits_for_settled_not_prompt_ack_or_agent_end(rpc_client):
    pi = rpc_client
    seen = []

    async def event(value):
        seen.append(value["type"])

    try:
        await pi.start()
        result = await pi.run("work", event, lambda: False)
        assert result["text"] == "finished after retry"
        assert seen == ["agent_end", "auto_retry_start", "auto_retry_end", "agent_settled"]
    finally:
        await pi.close()


async def test_stop_clears_queue_before_abort_and_closes_process(rpc_client):
    pi = rpc_client
    try:
        await pi.start()
        result = await pi.run("hang", lambda event: asyncio.sleep(0), lambda: True)
        assert result["stopped"]
        commands = (pi.config.data_dir / "runtime/test/commands.txt").read_text().splitlines()
        assert commands.index("clear_queue") < commands.index("abort")
    finally:
        await pi.close()
    assert pi.process.returncode is not None
