"""Comprueba una API PMV1 real, sin servicios sustituidos ni claves en argumentos."""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
from uuid import UUID

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.application.dto.evaluacion_ia_dto import DictamenIAResponse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True, help='URL del backend autorizado')
    parser.add_argument('--project-ref', required=True, help='EXP existente o UUID')
    parser.add_argument('--output', default='artifacts/ia-live.json')
    args = parser.parse_args()
    parsed = urlparse(args.base_url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('La URL debe ser HTTP(S), sin credenciales ni parametros')
    token = os.environ.get('SIPRIM_ACCESS_TOKEN', '').strip()
    if not token:
        parser.error('Falta SIPRIM_ACCESS_TOKEN en el entorno; no escriba tokens en Git o argumentos')
    report = {'fecha_utc': datetime.now(timezone.utc).isoformat(), 'entorno': 'HTTP real, sin mocks; requiere proyecto y cuenta de prueba autorizados', 'resultado': 'fallo', 'pasos': []}
    try:
        report['commit_cliente'] = subprocess.check_output(['git','rev-parse','HEAD'], text=True).strip()
    except (OSError, subprocess.SubprocessError):
        report['commit_cliente'] = None
    try:
        with httpx.Client(base_url=args.base_url.rstrip('/'), timeout=65) as client:
            def call(name, method, path, headers=None):
                start = time.monotonic()
                response = client.request(method, path, headers=headers)
                # Solo estados/tiempos. Nunca tokens, ficha privada o errores crudos.
                report['pasos'].append({'caso': name, 'http': response.status_code, 'segundos': round(time.monotonic()-start,3)})
                return response
            try:
                project_uuid = str(UUID(args.project_ref))
                path = f'/api/v1/projects/{project_uuid}'
            except ValueError:
                if not (3 <= len(args.project_ref) <= 40) or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for c in args.project_ref):
                    raise ValueError('Referencia de expediente invalida')
                path = '/api/v1/projects/by-code/' + quote(args.project_ref, safe='')
            headers = {'Authorization': 'Bearer ' + token}
            rejected = call('Acceso sin sesion', 'GET', path)
            if rejected.status_code != 401:
                raise ValueError('La ruta no rechazo el acceso anonimo con 401')
            project = call('Resolver EXP o UUID', 'GET', path, headers)
            project.raise_for_status()
            project_uuid = str(UUID(project.json()['project_id']))
            result = call('Evaluar expediente PMV1', 'POST', f'/api/v1/projects/{project_uuid}/evaluacion-ia', headers)
            if not result.is_success:
                try:
                    detail = result.json().get('detail', {})
                    code = detail.get('code') if isinstance(detail, dict) else None
                    if isinstance(code, str) and code.startswith('IA_') and len(code) < 60:
                        report['codigo_error_ia'] = code
                except (ValueError, AttributeError):
                    pass
            result.raise_for_status()
            DictamenIAResponse.model_validate(result.json())
            report['resultado'] = 'aprobado'
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        report['error_tipo'] = type(exc).__name__
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(f"Resultado: {report['resultado']}; evidencia: {out}")
    return 0 if report['resultado'] == 'aprobado' else 1


if __name__ == '__main__':
    raise SystemExit(main())
