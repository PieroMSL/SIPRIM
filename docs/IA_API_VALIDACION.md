# Validación y evidencia de integración IA API

## Alcance de esta corrección

La ruta oficial `POST /api/v1/projects/{UUID}/evaluacion-ia` sigue utilizando
JWT, el expediente PMV1 y `GeminiAdapter` con LangGraph. EXP se resuelve primero
con `GET /api/v1/projects/by-code/{codigo}`. No se agrega una demostración que
reemplace ese recorrido ni un dictamen predefinido en la aplicación.

Se corrigen dos fallos:

- Un expediente con magnitudes no positivas o textos en blanco devuelve 422 con
  `missing_fields` antes de invocar IA. Antes podía terminar como fallo interno del grafo.
- Backend y frontend rechazan un dictamen inválido, no finito o contradictorio.
  Las bandas existentes del prompt (70/40) ahora se verifican en ambos contratos.
  Son bandas orientativas del software, no estándares profesionales de ingeniería.

## Pruebas automáticas

Desde la raíz, con dependencias instaladas:

```sh
python -m pytest --junitxml=artifacts/pytest.xml
npm --prefix frontend ci
npm --prefix frontend run test:ia
npm --prefix frontend run build
npm --prefix frontend run lint
```

`backend/tests/test_ia_failure_contracts.py` ejecuta el controlador PMV1, la
validación JWT real, el grafo y el DTO. Sustituye el almacenamiento y la frontera
de red del SDK exclusivamente dentro de las pruebas para ensayar 401/403/429/504/500,
salida vacía, JSON inválido y lentitud. Estas son pruebas ejecutables de fallos
controlados; sus resultados no se presentan como incidentes reales de Google ni
como una evaluación de proyectos municipales reales. Ninguno de esos sustitutos
se instala en la aplicación o el despliegue.

La prueba EXP → UUID → grafo verifica código, UUID y análisis técnico en el
contexto enviado al SDK. `frontend/tests/ia.test.mjs` verifica los contratos que
consume la pantalla, incluidos los límites 40 y 70. No equivale a una grabación
extremo a extremo con PostgreSQL y Google.

El workflow `.github/workflows/ia-api.yml` ejecuta estos controles en PR o por
solicitud manual. No requiere claves ni despliega el proyecto.

## Comprobación contra un backend real

`scripts/verify_ia_live.py` llama al backend indicado sin sustituir servicios.
Requiere un expediente académico ya registrado y un token PLANNER autorizado
(o SUPERADMIN permitido por el código). Se configura `SIPRIM_ACCESS_TOKEN` en el
entorno privado, nunca como argumento, archivo versionado o mensaje.

```sh
python scripts/verify_ia_live.py --base-url http://127.0.0.1:8000 --project-ref EXP-ABC123 --output artifacts/ia-live.json
```

Comprueba el rechazo anónimo 401, resuelve la referencia y solicita el dictamen
real validando el esquema. Sale con código 1 ante fallo y 0 solo si todo pasa.
El JSON conserva estados y tiempos, no el token ni la ficha privada. El SHA es
el del cliente que ejecuta el script; no certifica el SHA del servidor.
Esta comprobación puede consumir una llamada al proveedor. No crea expedientes,
no cambia credenciales y no agota cuota deliberadamente.

## Criterios, fuentes y revisión del experto

El mínimo implementado comprende nombre, descripción/objetivo, ubicación,
tipo/uso propuesto, presupuesto positivo y beneficiarios enteros positivos.
Son requisitos para solicitar una preevaluación. No acreditan que un expediente
sea técnicamente viable ni sustituyen memoria descriptiva, metrados, cronograma,
costos unitarios y revisión profesional.

La revisión registrada por el grupo identifica a Job Daniel Gamarra Moreno:
https://trello.com/c/62b47lQs. Recomienda información municipal oficial, fuentes
verificables, validación de cada agente y del coordinador, métricas pertinentes
y control de consumo. La implementación conserva temperatura 0 y el aviso de
revisión humana. La aceptación profesional de fuentes concretas, sus versiones,
criterios y umbrales sigue pendiente: no se atribuye ese acuerdo al revisor.

Para registrar esa conformidad deben consignarse documento/version/localizador,
datos técnicos requeridos, regla de evaluación, método de contraste, nombre del
revisor y fecha. El ejemplo oral del 80 % no se convierte en meta de exactitud.

## Evidencia y video

Los resultados ejecutados se conservan en `docs/evidencias/ia-api-2026-10-23/`.
El reporte distingue pruebas controladas, comprobaciones reales y pendientes.

Video previo del grupo: https://drive.google.com/file/d/1QCu43-lLAsERycVoqQsvQDSw-Nt4GpAW/view
Duración verificada: 129.83 s. El tramo final muestra la demostración temporal
con PRY y 55/100, MEDIA. No acredita el flujo completo EXP/UUID con Gemini ni
los fallos de cuota/salida inválida. No se rotula como grabado en esta corrección.

Para la evidencia visual restante, grabar la pantalla del frontend de esta rama:

1. Mostrar versión utilizada y sesión de una cuenta de prueba autorizada.
2. Consultar un EXP existente; mostrar el UUID resuelto y solicitar IA.
3. Mostrar dictamen, límites y aviso de revisión humana.
4. Mostrar un error auténtico de entrada o sesión en el mismo recorrido.
5. Guardar el original con fecha, entorno, SHA y consentimiento sobre los datos.

La grabación y la conformidad humana no se sustituyen con capturas generadas,
resultados de unit tests o una casilla marcada. El desarrollo de software y la
evidencia profesional pendiente tienen estados distintos.
