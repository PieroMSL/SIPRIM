"""Pruebas automatizadas de fallos controlados; no incidentes del proveedor real."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import jwt
import pytest
from fastapi.testclient import TestClient
from google.genai.errors import ClientError
from pydantic import ValidationError

from backend.application.dto.evaluacion_ia_dto import DictamenIAResponse
from backend.infrastructure.config.dependencies import get_platform_service, get_ia_service
from backend.infrastructure.config.settings import get_settings
from backend.infrastructure.output.ai.gemini_adapter import GeminiAdapter
from backend.main import create_app

PROJECT_ID = UUID('a0000000-0000-4000-a000-00000000000a')
FICHA = dict(nombre='Proyecto de prueba', descripcion='Ficha academica de parque', ubicacion='El Tambo referencial', presupuesto=120000.0, beneficiarios=600, tipo_proyecto='Parque')
VALID = dict(puntaje=35.0, viabilidad='BAJA', justificacion='Faltan documentos tecnicos para comprobar viabilidad.', observaciones=['Caso de prueba'], recomendaciones=['Revision humana'])


class PlatformFixture:
    """Solo almacenamiento de pruebas; autenticacion, grafo y DTO son reales."""
    def __init__(self):
        self.project = dict(project_id=PROJECT_ID, project_version_id=UUID('b0000000-0000-4000-a000-00000000000a'), code='EXP-TEST01', version_number=1, status='ready', title=FICHA['nombre'], description=FICHA['descripcion'], location=FICHA['ubicacion'], proposed_land_use=FICHA['tipo_proyecto'], territorial_data_origin='declared', estimated_budget_pen=Decimal('120000'), beneficiaries_count=600)

    def get_project(self, project_id):
        assert project_id == PROJECT_ID
        return self.project

    def get_project_by_code(self, code):
        assert code == 'EXP-TEST01'
        return self.project


def authorization(role='PLANNER', expired=False):
    s = get_settings()
    token = jwt.encode({'sub': '10000000-0000-4000-a000-000000000002', 'roles': [role], 'iss': s.jwt_issuer, 'aud': s.jwt_audience, 'exp': datetime.now(timezone.utc) + timedelta(seconds=-10 if expired else 60)}, s.jwt_secret.get_secret_value(), algorithm='HS256')
    return {'Authorization': f'Bearer {token}'}


def sdk_response(monkeypatch, text=None, error=None, delay=0):
    """Sustituye la frontera de red del SDK; nunca se usa en la aplicacion."""
    calls = []
    class Models:
        async def generate_content(self, **kwargs):
            calls.append(kwargs)
            if delay:
                await asyncio.sleep(delay)
            if error:
                raise error
            return SimpleNamespace(text=text)
    class Client:
        def __init__(self, **kwargs):
            self.aio = self
            self.models = Models()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
    monkeypatch.setattr('backend.infrastructure.output.ai.gemini_adapter.genai.Client', Client)
    return calls


def client_for(adapter, platform=None):
    app = create_app()
    app.dependency_overrides[get_platform_service] = lambda: platform or PlatformFixture()
    app.dependency_overrides[get_ia_service] = lambda: adapter
    return TestClient(app)


@pytest.mark.parametrize('code,expected,status', [(401,'IA_AUTH_ERROR',503),(403,'IA_AUTH_ERROR',503),(429,'IA_RATE_LIMIT',429),(504,'IA_TIMEOUT',504),(500,'IA_PROVIDER_ERROR',502)])
def test_provider_errors_are_safe_http_errors(monkeypatch, code, expected, status):
    sdk_response(monkeypatch, error=ClientError(code, {'error': {'code': code, 'message': 'private-provider-detail-do-not-return'}}))
    with client_for(GeminiAdapter('unit-test-not-a-secret', 'test-model')) as client:
        r = client.post(f'/api/v1/projects/{PROJECT_ID}/evaluacion-ia', headers=authorization())
    assert r.status_code == status
    assert r.json()['detail']['code'] == expected
    assert 'private-provider-detail' not in r.text
    assert 'unit-test-not-a-secret' not in r.text
    assert 'puntaje' not in r.json()


@pytest.mark.parametrize('body,expected', [(None,'IA_EMPTY_RESPONSE'),('  ','IA_EMPTY_RESPONSE'),('not json','IA_INVALID_RESPONSE'),('{}','IA_INVALID_RESPONSE'),(json.dumps({**VALID,'viabilidad':'ALTA'}),'IA_INVALID_RESPONSE')])
def test_empty_invalid_or_contradictory_output_is_not_a_dictamen(monkeypatch, body, expected):
    sdk_response(monkeypatch, text=body)
    with client_for(GeminiAdapter('unit-test-not-a-secret', 'test-model')) as client:
        r = client.post(f'/api/v1/projects/{PROJECT_ID}/evaluacion-ia', headers=authorization())
    assert r.status_code == 502
    assert r.json()['detail']['code'] == expected


def test_deadline_cancels_slow_sdk(monkeypatch):
    sdk_response(monkeypatch, text=json.dumps(VALID), delay=1)
    with client_for(GeminiAdapter('unit-test-not-a-secret', 'test-model', timeout_seconds=.05)) as client:
        r = client.post(f'/api/v1/projects/{PROJECT_ID}/evaluacion-ia', headers=authorization())
    assert r.status_code == 504
    assert r.json()['detail']['code'] == 'IA_TIMEOUT'


@pytest.mark.parametrize('headers,status', [({},401),({'Authorization':'Bearer invalid-test-token'},401),(authorization(expired=True),401),(authorization('LEGAL_ADVISOR'),403)])
def test_pmv1_uses_real_jwt_validation(headers, status):
    with client_for(GeminiAdapter(None, 'test-model')) as client:
        r = client.post(f'/api/v1/projects/{PROJECT_ID}/evaluacion-ia', headers=headers)
    assert r.status_code == status


@pytest.mark.parametrize('field,value', [('estimated_budget_pen',0),('estimated_budget_pen',-1),('beneficiaries_count',0),('beneficiaries_count',-1),('location','   '),('description','   ')])
def test_invalid_technical_input_never_calls_provider(monkeypatch, field, value):
    calls = sdk_response(monkeypatch, text=json.dumps(VALID))
    platform = PlatformFixture()
    platform.project[field] = value
    with client_for(GeminiAdapter('unit-test-not-a-secret', 'test-model'), platform) as client:
        r = client.post(f'/api/v1/projects/{PROJECT_ID}/evaluacion-ia', headers=authorization())
    assert r.status_code == 422
    assert field in r.json()['missing_fields']
    assert not calls


def test_exp_resolves_uuid_and_runs_graph_and_contract(monkeypatch):
    calls = sdk_response(monkeypatch, text=json.dumps(VALID))
    with client_for(GeminiAdapter('unit-test-not-a-secret', 'test-model')) as client:
        found = client.get('/api/v1/projects/by-code/EXP-TEST01', headers=authorization())
        assert found.status_code == 200
        project_id = found.json()['project_id']
        r = client.post(f'/api/v1/projects/{project_id}/evaluacion-ia', headers=authorization())
    assert r.status_code == 200
    assert r.json() == VALID
    context = json.loads(calls[0]['contents'])
    assert context['proyecto']['id'] == str(PROJECT_ID)
    assert context['proyecto']['codigo'] == 'EXP-TEST01'
    assert 'S/ 200.00' in context['analisis_tecnico']
    assert 'no constituye aprobacion' in context['analisis_coordinador']


@pytest.mark.parametrize('patch', [{'puntaje':float('nan')},{'puntaje':101},{'observaciones':[' ']},{'justificacion':' ' * 20}])
def test_public_contract_rejects_unsafe_payloads(patch):
    with pytest.raises(ValidationError):
        DictamenIAResponse.model_validate({**VALID, **patch})
