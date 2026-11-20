-- ============================================================================
-- HU1.10 · Verificar Ley de Contrataciones (Ley N.° 32069 y D.S. N.° 009-2025-EF)
-- Actualiza fragmentos normativos vigentes según Compendio oficial OECE,
-- preserva normas sustituidas exclusivamente como históricas (in_force = FALSE)
-- y crea tablas versionadas para antecedentes contractuales y su verificación.
-- ============================================================================

ALTER TABLE IF EXISTS normative_documents
    ADD COLUMN IF NOT EXISTS official_url TEXT,
    ADD COLUMN IF NOT EXISTS consulted_at DATE,
    ADD COLUMN IF NOT EXISTS amendments_summary TEXT,
    ADD COLUMN IF NOT EXISTS effective_from DATE;

-- Actualizar fragmentos vigentes de la Ley N.° 32069 conforme a la compilación oficial OECE/SPIJ (al 19/07/2026)
INSERT INTO normative_documents (
    id, document_name, short_code, version, topic, content,
    in_force, has_alert, data_origin, official_url, consulted_at, amendments_summary, effective_from
) VALUES
    (
        'LEY32069-01',
        'Ley N.° 32069 - Ley General de Contrataciones Públicas',
        'LEY_32069',
        '2026-07-19-OECE',
        'Principios rectores de la contratación pública (Art. 5)',
        'Art. 5.1.- Principios rectores de la contratación pública: a) Legalidad, b) Eficacia y eficiencia (garantiza la calidad técnica de expedientes técnicos, especificaciones técnicas y términos de referencia), c) Valor por dinero (maximiza valor considerando calidad, sostenibilidad, costos y plazos, no únicamente menor precio), d) Integridad, g) Publicidad, i) Transparencia y facilidad de uso. Vigente desde el 22/04/2025 conforme a D.S. N.° 009-2025-EF.',
        TRUE,
        FALSE,
        'public',
        'https://www.gob.pe/institucion/oece/colecciones/45029-ley-n-32069-ley-general-de-contrataciones-publicas',
        DATE '2026-11-20',
        'Ley N.° 32069 vigente desde 22/04/2025; texto actualizado SPIJ/OECE al 19/07/2026 (Leyes 32103, 32185, 32187, 32515, D.Leg. 1715, Ley 32732).',
        DATE '2025-04-22'
    ),
    (
        'LEY32069-02',
        'Ley N.° 32069 - Ley General de Contrataciones Públicas',
        'LEY_32069',
        '2026-07-19-OECE',
        'Actuaciones preparatorias y elaboración del requerimiento (Art. 46 y Art. 50)',
        'Art. 46.1.- El requerimiento da inicio al proceso de contratación en el marco de la PMBSO y etapas presupuestarias, considerando valor por dinero; el área usuaria o área técnica estratégica lo determina en coordinación con la DEC identificando finalidad pública y objetivos. Art. 46.3.- Permite acceso en igualdad sin direccionar ni obstaculizar competencia. Art. 46.4.- En bienes se plasma en especificaciones técnicas; en servicios en TDR; y en obras en expediente técnico u objetivos funcionales con estructura de costos (Art. 46.5). Art. 50.1.- Prohibido fraccionar contratación de bienes, servicios u obras para evadir procedimiento de selección o evitar aplicación de la ley.',
        TRUE,
        FALSE,
        'public',
        'https://www.gob.pe/institucion/oece/informes-publicaciones/6444155-ley-n-32069-ley-general-de-contrataciones-publicas-y-su-reglamento-actualizados',
        DATE '2026-11-20',
        'Compendio OECE/SPIJ actualizado al 19/07/2026; concordado con D.S. N.° 009-2025-EF Arts. 44 y 45.',
        DATE '2025-04-22'
    ),
    (
        'LEY32069-03',
        'Ley N.° 32069 - Ley General de Contrataciones Públicas',
        'LEY_32069',
        '2026-07-19-OECE',
        'Contratos menores y procedimientos de selección competitivos (Arts. 34, 53 y 54)',
        'Art. 34.1.- Contratos menores: montos iguales o inferiores a ocho (8) UIT vigentes al momento de contratación (UIT 2026 = S/ 5,500.00 según D.S. N.° 301-2025-EF; tope 8 UIT = S/ 44,000.00), sin procedimiento de selección y sujetos a supervisión del OECE y registro en Pladicop (Art. 34.2). Art. 53.- El procedimiento de selección se determina según objeto contractual y cuantía conforme al Reglamento. Art. 54.1.- Procedimientos de selección competitivos: a) Licitación pública para contratación de bienes y obras; b) Concurso público para contratación de servicios. Art. 54.2.- Modalidades abreviadas y diferenciadas en el Reglamento. Art. 54.3.- Regla general de selección competitiva bajo valor por dinero, salvo excepciones legales.',
        TRUE,
        FALSE,
        'public',
        'https://www.gob.pe/institucion/oece/colecciones/45029-ley-n-32069-ley-general-de-contrataciones-publicas',
        DATE '2026-11-20',
        'Concordado con D.S. N.° 301-2025-EF (UIT 2026 = S/ 5,500.00) y D.S. N.° 009-2025-EF mod. D.S. N.° 001-2026-EF.',
        DATE '2025-04-22'
    ),
    (
        'LEY32069-04',
        'Ley N.° 32069 - Ley General de Contrataciones Públicas',
        'LEY_32069',
        '2026-07-19-OECE',
        'Ejecución contractual, modificaciones y prestaciones adicionales (Arts. 63 y 64)',
        'Art. 63.1.- Modificaciones contractuales aprobadas por autoridad de gestión administrativa para alcanzar finalidad oportunamente sin afectar equilibrio económico-financiero (Art. 63.2). Art. 64.1.- Bienes, servicios y consultorías: adicionales hasta 25%. Art. 64.2.- Obras bajo solo construcción: autoridad de gestión administrativa autoriza adicionales hasta 15% descontando deductivos vinculados. Art. 64.3.- Excepciones en obras: a) Titular de entidad >15% hasta 30%; b) Titular de entidad >30% hasta máximo 50% previa autorización de la Contraloría General de la República.',
        TRUE,
        FALSE,
        'public',
        'https://www.gob.pe/institucion/oece/informes-publicaciones/6444155-ley-n-32069-ley-general-de-contrataciones-publicas-y-su-reglamento-actualizados',
        DATE '2026-11-20',
        'Texto oficial Art. 64.2 y 64.3 Ley N.° 32069 (tramos 15%, 15%-30% Titular y 30%-50% CGR).',
        DATE '2025-04-22'
    ),
    (
        'LEY32069-05',
        'Ley N.° 32069 - Ley General de Contrataciones Públicas',
        'LEY_32069',
        '2026-07-19-OECE',
        'Solución de controversias, arbitraje y medidas cautelares (Arts. 76, 84 y 85)',
        'Art. 76.1.- Controversias en ejecución contractual se resuelven vía JPRD, conciliación, arbitraje o mecanismos de contratos estandarizados. Art. 84.1.- Arbitraje de derecho e institucional en registro OECE (ad hoc solo <= 10 UIT). Art. 85.1.e (mod. por Art. 3 D.Leg. N.° 1715 pub. 04/02/2026).- No proceden medidas cautelares para impedir o paralizar obras en salud, educación, infraestructura vial, saneamiento e infraestructura hidráulica.',
        TRUE,
        FALSE,
        'public',
        'https://www.gob.pe/institucion/mef/normas-legales/8403069-32732',
        DATE '2026-11-20',
        'Incluye modificación del D.Leg. N.° 1715 (04/02/2026) al Art. 85.1 literal e.',
        DATE '2025-04-22'
    ),
    (
        'LEY32069-06',
        'Ley N.° 32069 - Ley General de Contrataciones Públicas',
        'LEY_32069',
        '2026-07-19-OECE',
        'Plataforma Digital para las Contrataciones Públicas - PLADICOP (Art. 41 mod. Ley 32187)',
        'Art. 41.1 (mod. Décima Segunda DCM Ley N.° 32187, vig. 01/01/2025).- Pladicop es el conjunto de herramientas digitales que permiten la gestión e integración de la información sobre contrataciones dentro del ámbito del SNA. Art. 41.2 (mod. Ley N.° 32187).- Las entidades contratantes están obligadas a registrar la información requerida sobre el proceso de contratación. Art. 41.4.- Los actos realizados en la plataforma digital tienen plena validez y sustituyen a los manuales, notificándose el mismo día.',
        TRUE,
        FALSE,
        'public',
        'https://www.gob.pe/institucion/mef/normas-legales/6280777-32187',
        DATE '2026-11-20',
        'Numerales 41.1, 41.2 y 41.5 modificados por Ley N.° 32187 (pub. 11/12/2024, vig. 01/01/2025).',
        DATE '2025-04-22'
    ),
    (
        'DS009-2025-REG-44',
        'Reglamento de la Ley N.° 32069 (D.S. N.° 009-2025-EF act. 26/10/2026)',
        'REG_32069',
        '2026-10-26-OECE',
        'Requerimiento, no fraccionamiento y estrategia de contratación (Arts. 44, 45 y 46)',
        'Reglamento Art. 44.2.- El requerimiento remitido por el área usuaria a la DEC incluye alcance y condiciones de ejecución, calificación, modalidad de pago y sistema de entrega. Reglamento Art. 45.1.- Prohibición y deslinde de responsabilidad por fraccionamiento (Art. 50 Ley 32069). Reglamento Art. 46.1.c.- La estrategia de contratación verifica la declaración de viabilidad para contrataciones que forman parte de un proyecto de inversión (Invierte.pe) o aprobación de IOARR.',
        TRUE,
        FALSE,
        'public',
        'https://www.gob.pe/institucion/oece/informes-publicaciones/6444155-ley-n-32069-ley-general-de-contrataciones-publicas-y-su-reglamento-actualizados',
        DATE '2026-11-20',
        'D.S. N.° 009-2025-EF (vig. 22/04/2025) actualizado al 26/10/2026 con D.S. N.° 001-2026-EF (08/01/2026) y D.S. N.° 222-2026-EF (21/10/2026).',
        DATE '2025-04-22'
    ),
    (
        'DS009-2025-REG-54',
        'Reglamento de la Ley N.° 32069 (D.S. N.° 009-2025-EF act. 26/10/2026)',
        'REG_32069',
        '2026-10-26-OECE',
        'Cuantía, expediente de contratación, CMN y contratos menores (Arts. 53, 54, 226 y 227)',
        'Reglamento Art. 53 (mod. D.S. N.° 001-2026-EF).- Cuantía de la contratación en el CMN y agregación de ítems/lotes/tramos. Reglamento Art. 54.2 y 54.3.- Para aprobar el expediente de contratación la necesidad debe constar en el CMN aprobado e incluir requerimiento final, estrategia, cuantía y certificación de crédito presupuestario o previsión presupuestal. Reglamento Art. 226.2 y 227.- Contratos menores (<= 8 UIT) requieren CMN, requerimiento y registro integral en Pladicop.',
        TRUE,
        FALSE,
        'public',
        'https://www.gob.pe/institucion/mef/normas-legales/7601483-001-2026-ef',
        DATE '2026-11-20',
        'Incluye modificaciones del D.S. N.° 001-2026-EF (08/01/2026) y D.S. N.° 222-2026-EF (21/10/2026).',
        DATE '2025-04-22'
    ),
    -- Fuentes históricas sustituidas o derogadas: conservadas estrictamente como históricas (in_force = FALSE)
    (
        'LEY30225-HIST',
        'Ley N.° 30225 - Ley de Contrataciones del Estado (Sustituida el 22/04/2025 por Ley N.° 32069)',
        'LEY_30225',
        '2019-TUO-derogada',
        'Régimen contractual anterior derogado por Ley N.° 32069 (TUO D.S. 082-2019-EF - Sustituida)',
        'Antecedente histórico sustituido expresamente desde el 22/04/2025 tras la entrada en vigor de la Ley N.° 32069 y D.S. N.° 009-2025-EF. No aplicable para procedimientos nuevos; se conserva únicamente para contraste o verificación transitoria por Asesoría Jurídica.',
        FALSE,
        FALSE,
        'public',
        'https://www.gob.pe/institucion/oece/colecciones/45029-ley-n-32069-ley-general-de-contrataciones-publicas',
        DATE '2026-11-20',
        'Sustituida por Ley N.° 32069 desde el 22/04/2025.',
        DATE '2019-03-13'
    ),
    (
        'LEY32069-2024V1-HIST',
        'Ley N.° 32069 - Texto original publicado 24/06/2024 pre-vigencia (Histórica - Sustituida)',
        'LEY_32069_HIST',
        '2024-v1-historica',
        'Texto primigenio de Ley 32069 previo a entrada en vigor (22/04/2025) y modificatorias Ley 32187',
        'Versión referencial histórica previa a la vigencia efectiva de la Ley N.° 32069 (22/04/2025) y a las modificaciones de la Ley N.° 32187 al Art. 41 (Pladicop) y D.Leg. 1715. Conservada únicamente como registro histórico; excluida de evaluaciones vigentes.',
        FALSE,
        FALSE,
        'public',
        'https://www.gob.pe/institucion/oece/colecciones/45029-ley-n-32069-ley-general-de-contrataciones-publicas',
        DATE '2026-11-20',
        'Superada por entrada en vigencia (22/04/2025) y modificaciones Ley 32187 / Ley 32515 / D.Leg. 1715 / Ley 32732.',
        DATE '2024-06-24'
    )
ON CONFLICT (id) DO UPDATE SET
    document_name = EXCLUDED.document_name,
    short_code = EXCLUDED.short_code,
    version = EXCLUDED.version,
    topic = EXCLUDED.topic,
    content = EXCLUDED.content,
    in_force = EXCLUDED.in_force,
    has_alert = EXCLUDED.has_alert,
    data_origin = EXCLUDED.data_origin,
    official_url = EXCLUDED.official_url,
    consulted_at = EXCLUDED.consulted_at,
    amendments_summary = EXCLUDED.amendments_summary,
    effective_from = EXCLUDED.effective_from;

UPDATE normative_documents
SET official_url = 'https://www.gob.pe/institucion/oece/colecciones/45029-ley-n-32069-ley-general-de-contrataciones-publicas',
    consulted_at = DATE '2026-11-20',
    amendments_summary = 'Norma derogada históricamente; sustituida por Ley 30225 y luego por Ley 32069 (vig. 22/04/2025).'
WHERE id = 'LEY26850-HIST';

-- Antecedentes contractuales estructurados por versión de expediente (sin inferir de título/descripción)
CREATE TABLE IF NOT EXISTS project_procurement_records (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_version_id UUID NOT NULL REFERENCES project_versions(id) ON DELETE CASCADE UNIQUE,
    contracting_regime VARCHAR(40),
    contract_object VARCHAR(32),
    contracting_phase VARCHAR(40),
    selection_procedure VARCHAR(64),
    requirement_defined BOOLEAN,
    in_cmn_and_viability BOOLEAN,
    budget_certification_declared BOOLEAN,
    pladicop_registration_planned BOOLEAN,
    no_fractionation_declared BOOLEAN,
    supporting_document_ref VARCHAR(300),
    contractual_summary TEXT,
    data_origin VARCHAR(16) NOT NULL DEFAULT 'declared'
        CHECK (data_origin IN ('declared', 'simulated', 'public')),
    updated_by_user_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_procurement_records_proj
    ON project_procurement_records(project_id, project_version_id);

-- Verificaciones persistidas HU1.10 por versión y trazabilidad hacia Asesoría Jurídica (LEGAL_ADVISOR)
CREATE TABLE IF NOT EXISTS procurement_verification_requests (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_version_id UUID NOT NULL REFERENCES project_versions(id) ON DELETE CASCADE,
    requested_by_user_id UUID NOT NULL,
    idempotency_key VARCHAR(120) NOT NULL UNIQUE,
    status VARCHAR(36) NOT NULL
        CHECK (status IN ('verified_preliminary', 'possible_incompatibility', 'requires_review')),
    status_label VARCHAR(160) NOT NULL,
    preliminary_compliant BOOLEAN,
    requires_human_review BOOLEAN NOT NULL DEFAULT TRUE,
    certified_infraction BOOLEAN NOT NULL DEFAULT FALSE,
    sent_to_legal_advisor BOOLEAN NOT NULL DEFAULT FALSE,
    summary TEXT NOT NULL,
    affected_requirement TEXT,
    observation_explanation TEXT,
    missing_fields_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    examined_requirements_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    normative_sources_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    superseded_sources_excluded_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    advisory_review_status VARCHAR(32) NOT NULL DEFAULT 'pending'
        CHECK (advisory_review_status IN ('not_required', 'pending', 'validated_compatible', 'confirmed_incompatibility', 'subsanacion_requerida')),
    advisory_reviewer_id UUID,
    advisory_review_notes TEXT,
    advisory_reviewed_at TIMESTAMPTZ,
    consulted_at DATE NOT NULL DEFAULT DATE '2026-11-20',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_hu110_no_certified_infraction CHECK (certified_infraction = FALSE),
    CONSTRAINT chk_hu110_requires_review_no_compliance CHECK (
        status <> 'requires_review' OR preliminary_compliant IS NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_proc_verif_proj_ver
    ON procurement_verification_requests(project_id, project_version_id, created_at DESC);

ALTER TABLE IF EXISTS legal_assessments
    ADD COLUMN IF NOT EXISTS procurement_verification_json JSONB;

-- Antecedentes contractuales base para el proyecto semilla PRY-A (v1), coherente con su hallazgo seeded en 02_seed.sql
INSERT INTO project_procurement_records (
    id, project_id, project_version_id, contracting_regime, contract_object,
    contracting_phase, selection_procedure, requirement_defined, in_cmn_and_viability,
    budget_certification_declared, pladicop_registration_planned, no_fractionation_declared,
    supporting_document_ref, contractual_summary, data_origin, updated_by_user_id
)
SELECT
    'f1000000-0000-4000-a000-000000000001'::uuid,
    'a0000000-0000-4000-a000-00000000000a'::uuid,
    'b0000000-0000-4000-a000-00000000000a'::uuid,
    'ley_32069',
    'obra',
    'actuaciones_preparatorias',
    'licitacion_publica',
    TRUE,
    TRUE,
    TRUE,
    TRUE,
    TRUE,
    'Exp. Técnico PRY-A / Requerimiento Área Usuaria - CMN 2026',
    'Ejecución de obra pública mediante licitación pública competitiva bajo Ley N.° 32069.',
    'public',
    '10000000-0000-4000-a000-000000000002'::uuid
WHERE EXISTS (
    SELECT 1 FROM project_versions WHERE id = 'b0000000-0000-4000-a000-00000000000a'::uuid
)
ON CONFLICT (project_version_id) DO NOTHING;
