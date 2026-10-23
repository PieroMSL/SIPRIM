# Reporte de cambios IA API del 23 de octubre de 2026

Base: `f0d1558c1b6c0e5636532c6d4b9d213357b6f9b2` de Marcce265/SIPRIM.
Rama: `fix/ia-api-validacion-evidencias`. El commit que contiene este reporte
identifica el conjunto de código y pruebas; el SHA del servidor publicado no
fue certificado.

## Correcciones implementadas

- DTO de ficha técnica con texto no vacío, presupuesto finito positivo y
  beneficiarios enteros positivos. La ruta UUID retorna 422 antes de llamar IA.
- Coherencia entre puntuación y bandas existentes de viabilidad en backend y
  frontend. Se rechazan valores no finitos, textos vacíos y propiedades ajenas.
- Contrato de frontend compartido y validación de datos HTTP desconocidos.
- Suite de fallos del SDK, JWT real, EXP → UUID → grafo, y salida inválida.
- Verificador HTTP real `scripts/verify_ia_live.py` con salida JSON saneada.
- Workflow de PR para ejecutar las pruebas y compilar el frontend sin claves.

## Comprobaciones ejecutadas

| Control | Resultado |
|---|---|
| pytest backend/tests | 81 aprobadas, 0 fallos |
| npm run test:ia | 19 aprobadas, 0 fallos |
| npm run build | Aprobado, incluye TypeScript |
| npm run lint | Aprobado con 3 advertencias preexistentes en otras pantallas |
| git diff --check | Aprobado |

`pytest.xml` y `frontend-tests.txt` contienen los registros ejecutados. Los
fallos controlados de la suite utilizan sustitutos del SDK exclusivamente en
pruebas; no son incidentes atribuidos a Google ni resultados municipales reales.
El almacén de la prueba EXP es una fixture, no una validación de PostgreSQL.

## Llamadas reales separadas

`proveedor-real.json` registra dos ejecuciones del adaptador sin sustituir el
SDK: rechazo de una cadena de prueba deliberadamente inválida y vencimiento de
un límite temporal local de 0.01 s. La primera devolvió IA_AUTH_ERROR; la segunda
IA_TIMEOUT. El límite local comprueba cancelación de la operación, no una caída
de Google. No se modificó una clave real ni se agotó cuota. No se provocaron
respuestas vacías o inválidas en Google: esas ramas se ensayaron bajo control.

En una ejecución previa de esta sesión sobre el despliegue temporal, datos
académicos ficticios produjeron PRY-18D622B4, COMPLETO, 35/100, BAJA. Esta ejecución
no prueba el flujo oficial EXP/UUID ni que este commit esté desplegado.

## Estado de las cuatro actividades

1. Manejo de errores: implementado y cubierto por pruebas automáticas. Se
   distinguen los fallos controlados de autenticación/timeout observados con el
   SDK real. No se afirma una cuota agotada o JSON inválido recibido de Google.
2. EXP/UUID: validación y prueba de integración controlada disponibles; verificador
   real listo. La ejecución completa con PostgreSQL y proveedor configurado aún
   necesita ese entorno y el token de una cuenta autorizada.
3. Criterios técnicos: mínimo implementado y fuentes/recomendaciones registradas
   en docs/IA_API_VALIDACION.md. La conformidad profesional es humana y sigue
   pendiente; un commit no puede crear ese acuerdo.
4. Reporte y commit: versionados. El video anterior enlazado en la guía fue
   inspeccionado (129.83 s), pero no cubre todo el recorrido/error de esta rama.
   No se fabricó una grabación nueva.
