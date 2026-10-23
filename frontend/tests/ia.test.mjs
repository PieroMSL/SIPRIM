import { test } from 'node:test'
import assert from 'node:assert/strict'
import { parseDictamenIA } from '../src/features/ia/dictamen.ts'
import { isValidExpedienteLookup, isExpedienteUuid } from '../src/utils/expedienteRef.ts'
const good = { puntaje: 35, viabilidad: 'BAJA', justificacion: 'Requiere revisión de documentos técnicos.', observaciones: ['Sin expediente'], recomendaciones: ['Revisión humana'] }
test('Acepta un dictamen válido conservando las advertencias', () => assert.deepEqual(parseDictamenIA(good), good))
for (const value of [null, '', {}, { ...good, puntaje: NaN }, { ...good, puntaje: Infinity }, { ...good, puntaje: 101 }, { ...good, viabilidad: 'ALTA' }, { ...good, justificacion: ' ' }, { ...good, observaciones: [''] }, { ...good, recomendaciones: [10] }, { ...good, secreto: 'dato ajeno' }]) {
  test(`Rechaza contrato inválido ${JSON.stringify(value)}`, () => assert.throws(() => parseDictamenIA(value), /dictamen inválido/))
}
for (const [puntaje, viabilidad] of [[0,'BAJA'],[39.9,'BAJA'],[40,'MEDIA'],[69.9,'MEDIA'],[70,'ALTA'],[100,'ALTA']]) {
 test(`Límite ${puntaje} corresponde a ${viabilidad}`, () => assert.equal(parseDictamenIA({ ...good, puntaje, viabilidad }).viabilidad, viabilidad))
}
test('Reconoce EXP y UUID sin aceptar texto arbitrario', () => {
 assert.ok(isValidExpedienteLookup('EXP-TEST01'))
 assert.ok(isExpedienteUuid('a0000000-0000-4000-a000-00000000000a'))
 assert.equal(isValidExpedienteLookup('código con espacios'), false)
})
