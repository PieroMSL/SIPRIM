# Guía de despliegue de SIPRIM para PMV3 y correcciones de PMV1

**Destinatario:** persona responsable de integrar y desplegar SIPRIM.  
**Fecha de preparación:** 20 de noviembre de 2026.  
**Repositorio oficial:** `Marcce265/SIPRIM`.  
**Estado de esta guía:** instrucciones de ejecución; no acredita que el despliegue se haya realizado.

## Objetivo y decisión de publicación

Publicar el avance local de PMV3 junto con las correcciones posteriores de PMV1 y PMV2 sin perder expedientes ni alterar la base de producción por accidente. La versión puede publicarse con dos límites visibles: HU1.3 muestra «No calculable» mientras no exista un método de retorno socioeconómico aprobado; HU1.11 muestra «Información insuficiente» cuando no hay zonificación territorial oficial verificable. Las pruebas sintéticas de HU1.11 no acreditan compatibilidad real.

**No integrar ni subir el código a `main` hasta completar los controles de esta guía.** El `main` oficial se sincroniza periódicamente con `PieroMSL/SIPRIM/main`, conectado a Vercel. El archivo `.github/workflows/sync-deployment-mirror.yml` programa la sincronización cada hora, al minuto 17 UTC. El proceso puede retrasarse; registrar el commit efectivamente desplegado.

Esta guía puede añadirse a `docs/` en un commit documental independiente. Su incorporación no equivale a autorizar el despliegue de los cambios funcionales.

## 1. Confirmar el entorno real antes de tocar datos

La documentación `docs/DEPLOYMENT_MIRROR.md` describe Vercel y bases **Neon**. El equipo también ha mencionado **Supabase**. El responsable debe identificar el proveedor y las bases que usa hoy el despliegue mediante la configuración vigente de Vercel, sin copiar contraseñas ni cadenas de conexión a GitHub, capturas, informes o registros. Confirmar las conexiones de `AUTH_DB_URL`, `PLATFORM_DB_URL` y `ECONOMIC_DB_URL`, las cuentas propietarias y los permisos de aplicación. No inferir que la base local o la citada en una guía antigua es producción.

Registrar el commit de producción, la última versión funcional disponible para reversión, el estado de la sincronización del espejo y quién ejecutará las migraciones. Si no se puede identificar con certeza la base real o recuperar un respaldo, detener el despliegue.

## 2. Preparar un conjunto de cambios revisable

1. En el repositorio local, comprobar rama, `git status`, remoto, commit actual y diferencia frente a `origin/main`. Hacer `fetch` para conocer los commits nuevos. Si hay divergencia o cambios ajenos, resolverla en una rama de preparación sin descartar trabajo local.
2. Revisar archivo por archivo lo que entrará al commit: código de backend y frontend, pruebas, migraciones y documentación/evidencias seleccionadas. El avance de PMV3 se encuentra en el commit local `f327b4c`; verificar su presencia respecto al remoto y mantener los cambios posteriores de PMV1/PMV2 y correcciones de pruebas en commits de alcance claro.
3. No añadir por comodidad todo el árbol con `git add .`. Excluir `scratch/`, `output/`, archivos temporales, volcados de bases, `.env`, credenciales, tokens, datos personales, dependencias generadas y evidencias repetidas o no relacionadas. Revisar expresamente las carpetas no registradas de `docs/`.
4. Verificar que los datos geoespaciales incluidos tienen procedencia y condiciones de uso documentadas. `database/geospatial/inei_el_tambo_120114/` contiene un límite distrital referencial del INEI; no presentarlo como zonificación predial.
5. Crear commits con alcance claro en la rama de preparación y validar antes de integrar a `main`. No usar `reset --hard`, `push --force` ni reemplazar archivos locales no registrados.

## 3. Revisar y ensayar las migraciones

Determinar con una comparación de esquemas qué migraciones faltan **realmente** en cada base desplegada. Partir del estado encontrado, no asumir que por haberse desplegado PMV2 todas las migraciones anteriores están aplicadas.

| Base | Archivos nuevos que deben comprobarse | Atención especial |
| --- | --- | --- |
| `auth_db` | `07_mayor_role.sql`, `08_planning_manager_role.sql`, `09_auditor_role.sql` | Crean roles; no crean usuarios de demostración. Verificar claves de rol existentes y permisos. |
| `platform_db` | `10_stored_procedures_u3.sql`, `11_migration_hu4.1_coordinates.sql`, `12_migration_hu4.2_geospatial_layers.sql`, `13_migration_hu3.3_checkpoint_decisions.sql` | Confirmar dependencias y necesidad real del procedimiento de carga masiva. No ejecutar cargas de prueba sobre producción. |
| `platform_db` | `14_migration_hu1.2_draft_nullable_budget_beneficiaries.sql`, `15_migration_hu1.1_multidimensional_dispatch.sql`, `16_migration_hu1.10_procurement_verification.sql` | La 14 cambia restricciones de presupuesto y beneficiarios (`NULL` permitido en borradores, `> 0` si se informan); revisar restricciones vigentes antes y después. La 16 crea tablas y actualiza fragmentos de `normative_documents` por identificador; comparar y respaldar esas filas antes de aplicarla. |

También comprobar migraciones anteriores a las listadas si faltan en el esquema real. Aplicar solo las faltantes en el orden de dependencias, primero en una copia o rama de prueba de la base desplegada. Ejecutar cada archivo con detención ante error y transacción cuando sea compatible. Ejecutar las migraciones con un usuario autorizado para DDL y comprobar después las concesiones de `auth_app`, `platform_app` y `economic_app` según corresponda.

**No ejecutar en producción** `database/bootstrap.sh`, `database/bootstrap_remoto.sh`, `01_schema.sql` completo, archivos `02_seed.sql`, pruebas masivas ni scripts de demostración. No sustituir expedientes ni documentos existentes. Antes de modificar producción, crear un respaldo nuevo, comprobar que puede restaurarse y dejar registrado su identificador y hora. Un respaldo anterior citado en `docs/DEPLOYMENT_MIRROR.md` no sustituye uno tomado para este despliegue.

## 4. Ejecutar pruebas antes de integrar a main

En PostgreSQL local aislado y, después, en una copia representativa del esquema desplegado:

- Ejecutar la **suite completa** de `backend/tests/` y registrar total de pruebas, fallos y omisiones. Los informes previos acreditan suites parciales; no sustituyen esta regresión final conjunta.
- Ejecutar `npm run typecheck`, `npm run lint` y `npm run build` en `frontend/`. Resolver fallos nuevos y dejar documentadas las advertencias conocidas (3 advertencias `react(set-state-in-effect)` preexistentes).
- Probar migraciones nuevas y existentes que falten con datos representativos. Comprobar que una segunda ejecución idempotente no duplica registros ni elimina datos. Confirmar recuentos, restricciones, índices, permisos y lectura por los roles de aplicación.
- Probar el backend con la configuración de producción prevista: `USE_IN_MEMORY_REPOSITORY=false`, `ENABLE_DEMO_FALLBACK=false` y `ENABLE_SYNTHETIC_ZONING_REHEARSAL=false`. No exponer valores secretos.
- Verificar autenticación y permisos para los roles relevantes, registro y consulta de expediente, despacho a cinco especialidades, evaluación, revisión jurídica, consulta normativa, verificación contractual, checkpoints y decisión humana, ranking/mapa y exportación PDF. Incluir un proyecto existente y uno nuevo. Validar que HU1.3 y HU1.11 comunican sus límites reales sin inventar resultados.
- Comprobar el comportamiento serverless de Vercel y las dependencias declaradas en `pyproject.toml` y `requirements.txt`. Un build exitoso o un estado `READY` no demuestran que la API y la base funcionen.

Si falla una prueba crítica, corregir y repetir el control afectado antes de publicar. No resolver una falla usando datos sintéticos como si fueran producción.

## 5. Preparar y ejecutar la publicación

1. Crear un despliegue de vista previa con el código candidato y bases de prueba. Probar en la URL de vista previa las rutas React, `/health`, la API, el inicio de sesión, los permisos y los recorridos principales. Revisar registros de Vercel y consultas reales a la base de prueba.
2. Confirmar el orden de compatibilidad entre código antiguo, esquema nuevo y código nuevo. Si el esquema nuevo es compatible con el despliegue anterior, aplicar primero las migraciones faltantes a producción **después del respaldo** y verificar salud y datos existentes. Si alguna migración rompe compatibilidad, planificar ventana de mantenimiento y promoción coordinada; no improvisar el orden.
3. Integrar el conjunto revisado en `Marcce265/SIPRIM/main` solo tras aprobar la vista previa y la preparación de datos. Verificar el SHA incorporado. Seguir el workflow de sincronización del espejo; comprobar que `PieroMSL/SIPRIM/main` llega al mismo SHA y que Vercel publica ese commit, no uno anterior.
4. Ejecutar de inmediato las pruebas de humo en el dominio final: página inicial, API, autenticación, permisos, consulta y creación de expediente, evaluación, revisión jurídica, consulta normativa, mapa/ranking y descarga de PDF. Abrir un expediente existente para confirmar que sus datos permanecen intactos. Revisar errores del servidor y conexión a la base.
5. Registrar fecha, operador, commits de origen/espejo, identificador del despliegue, respaldo, migraciones aplicadas, resultado de pruebas y límites funcionales publicados.

## 6. Actuar si el despliegue falla

Detener la promoción si fallan autenticación, acceso a expedientes, migraciones, permisos o API. Conservar evidencias del error sin incluir secretos. Revertir el código a un despliegue anterior conocido mediante el mecanismo de Vercel y coordinar la sincronización del espejo para evitar que el mismo commit defectuoso vuelva a publicarse. No asumir que revertir el código revierte la base: decidir una restauración de datos únicamente con el respaldo y un análisis del cambio aplicado. No usar `push --force`, `DROP` de tablas ni semillas para intentar reparar producción.

## Actualización de verificación (20 de noviembre de 2026)

- **Estado remoto y Vercel comprobado (solo lectura)**: `Marcce265/SIPRIM/main` y `PieroMSL/SIPRIM/main` apuntan ambos a `a54d31f5711bf0d155a7fed656c43297a8e714df` y `https://siprim-gemini-demo-piero-m.vercel.app/health` responde `HTTP 200 {"status":"ok"}`.
- **Estado local de pruebas (PostgreSQL 16.2 aislado)**: `348 passed` en `backend/tests/` (`325 passed` con `USE_IN_MEMORY_REPOSITORY=false`, `ENABLE_DEMO_FALLBACK=false`, `ENABLE_SYNTHETIC_ZONING_REHEARSAL=false`, más `23 passed` de suites heredadas en memoria con `USE_IN_MEMORY_REPOSITORY=true`); `test_predictive_ml.py` verificado también con `-X utf8=0`; `frontend` con `typecheck`, `lint` (`0` errores, `3` advertencias conocidas) y `build` aprobados.
- **Ensayo de migraciones incrementales**: `platform_db/14`, `15` y `16` aplicadas en doble ejecución idempotente sobre una base aislada con datos semilla previos (`01..13`), preservando registros y validando lectura por `platform_app`.
- **Bloqueo operativo pendiente antes de `push` a `origin/main`**: el entorno local no dispone de credenciales ni acceso CLI a Vercel, Supabase ni Neon (`bold-moon-78123050`), por lo que la identificación inequívoca de la base productiva vigente, la comparación de su esquema contra `auth_db/07..09` y `platform_db/10..16`, y la toma/prueba del respaldo previo quedan a cargo del responsable operativo antes de publicar `main`.

## Registro que debe completar la persona responsable

| Dato | Valor a registrar al ejecutar |
| --- | --- |
| Proveedor y bases de producción confirmados | Pendiente (ambigüedad Supabase / Neon sin acceso CLI/credenciales desde entorno local) |
| Commit anterior en producción | `a54d31f5711bf0d155a7fed656c43297a8e714df` (en `origin/main` y espejo `PieroMSL/SIPRIM/main`) |
| Commit candidato en `main` y SHA del espejo | Pendiente de push tras verificación de base y respaldo |
| Respaldo nuevo y prueba de restauración | Pendiente |
| Migraciones faltantes y aplicadas por base | Pendiente (`auth_db/07..09`, `platform_db/10..16` a contrastar contra esquema vivo) |
| Resultado de suite backend y verificación frontend | Backend `348 passed` (`325` PG + `23` memoria); Frontend `typecheck`/`lint`/`build` OK |
| URL y resultado de vista previa | Pendiente |
| Identificador y resultado del despliegue final | Pendiente |
| Responsable y fecha de aceptación | Pendiente |

**Referencias internas:** `docs/DEPLOYMENT_MIRROR.md`, `.github/workflows/sync-deployment-mirror.yml`, `database/README.md`, `database/bootstrap.sh` y `docs/historias/Criterios_Aceptacion_SIPRIM_26_HU_Depurados.docx`.
