from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import router as resources_router
from src.persistence import database
from sqlalchemy import select
from src.persistence.database import Base, ChatMessage, DurableTask, PlanStep, Run, configure_database, init_db
from src.runs.service import coordinator
from src.sessions.delivery import persist_terminal_response
from src.tasks.state import recovery_prompt


@pytest.fixture()
def client(tmp_path: Path):
    configure_database(f"sqlite:///{(tmp_path / 'resume.db').as_posix()}")
    init_db()
    app = FastAPI()
    app.include_router(resources_router)
    with TestClient(app) as test_client:
        yield test_client
    Base.metadata.drop_all(bind=database.engine)


def test_resume_task_starts_direct_recovery_run_without_user_message(client, monkeypatch):
    session_id = client.post('/api/sessions', json={}).json()['id']
    with database.SessionLocal() as db:
        task = DurableTask(session_id=session_id, goal='继续未完成任务', status='needs_recovery')
        db.add(task)
        db.flush()
        step = PlanStep(task_id=task.id, external_id='step-1', position=1, title='恢复执行', status='needs_recovery')
        previous = Run(session_id=session_id, task_id=task.id, status='failed', run_kind='initial')
        db.add_all([step, previous])
        db.flush()
        task.active_step_id = step.id
        db.commit()
        task_id = task.id
        previous_id = previous.id

    launched = []
    monkeypatch.setattr(coordinator, 'launch', lambda run_id: launched.append(run_id) or True)
    response = client.post(f'/api/sessions/{session_id}/tasks/{task_id}/resume')
    assert response.status_code == 202, response.text
    resumed_id = response.json()['id']

    assert launched == [resumed_id]
    with database.SessionLocal() as db:
        resumed = db.get(Run, resumed_id)
        assert resumed is not None
        assert resumed.task_id == task_id
        assert resumed.plan_step_id is not None
        assert resumed.run_kind == 'recovery'
        assert resumed.resumed_from_run_id == previous_id
        assert resumed.turn_id is not None
        assert db.scalar(
            select(ChatMessage).where(ChatMessage.session_id == session_id, ChatMessage.role == 'user')
        ) is None
        assert db.get(DurableTask, task_id).status == 'running'
        assert '继续未完成任务' in recovery_prompt(db, resumed)
        resumed.status = 'completed'
        message = persist_terminal_response(db, resumed, output='任务已恢复并完成')
        db.commit()
        assert message is not None
        assert message.role == 'assistant'
        assert message.content == '任务已恢复并完成'


@pytest.mark.parametrize('task_status', ['completed', 'cancelled', 'running', 'waiting'])
def test_resume_rejects_non_resumable_task(client, task_status):
    session_id = client.post('/api/sessions', json={}).json()['id']
    with database.SessionLocal() as db:
        task = DurableTask(session_id=session_id, goal='任务', status=task_status)
        db.add(task)
        db.commit()
        task_id = task.id
    assert client.post(f'/api/sessions/{session_id}/tasks/{task_id}/resume').status_code == 409


def test_resume_rejects_duplicate_and_other_session(client, monkeypatch):
    session_id = client.post('/api/sessions', json={}).json()['id']
    other_id = client.post('/api/sessions', json={}).json()['id']
    with database.SessionLocal() as db:
        task = DurableTask(session_id=session_id, goal='任务', status='paused')
        db.add(task)
        db.commit()
        task_id = task.id
    monkeypatch.setattr(coordinator, 'launch', lambda run_id: True)
    assert client.post(f'/api/sessions/{other_id}/tasks/{task_id}/resume').status_code == 404
    assert client.post(f'/api/sessions/{session_id}/tasks/{task_id}/resume').status_code == 202
    assert client.post(f'/api/sessions/{session_id}/tasks/{task_id}/resume').status_code == 409


def test_resume_schedule_failure_stays_retryable(client, monkeypatch):
    session_id = client.post('/api/sessions', json={}).json()['id']
    with database.SessionLocal() as db:
        task = DurableTask(session_id=session_id, goal='任务', status='paused')
        db.add(task)
        db.commit()
        task_id = task.id
    monkeypatch.setattr(coordinator, 'launch', lambda run_id: False)
    assert client.post(f'/api/sessions/{session_id}/tasks/{task_id}/resume').status_code == 503
    with database.SessionLocal() as db:
        assert db.get(DurableTask, task_id).status == 'needs_recovery'
        assert db.scalar(select(Run).where(Run.task_id == task_id)).status == 'failed'
