// node --test: the Voice check page's numbers (diagvoice.js): `kairo diag --json` as GET /api/diag/voice sends it.
import assert from 'node:assert/strict'
import { test } from 'node:test'

const { RATES, STAGES, configRows, groupChecks, machineRows, meter, ms, percent, stageBars, verdict } = await import('../src/diagvoice.js')

const check = (id, group, status) => ({ id, group, name: id, status, detail: 'd', value: null, unit: null })

test('groups keep the PC order, but the ones with a failure come first, then warnings', () => {
  const groups = groupChecks([
    check('a.1', 'A', 'PASS'), check('b.1', 'B', 'WARN'), check('c.1', 'C', 'PASS'), check('c.2', 'C', 'FAIL'),
    check('d.1', 'D', 'PASS'), check('b.2', 'B', 'PASS'),
  ])
  assert.deepEqual(groups.map((g) => [g.group, g.worst, g.checks.length]), [['C', 'FAIL', 2], ['B', 'WARN', 2], ['A', 'PASS', 1], ['D', 'PASS', 1]])
  assert.deepEqual(groupChecks(undefined), [])
})

test('the headline says healthy, healthy with warnings or not healthy', () => {
  assert.deepEqual(verdict({ counts: { pass: 30, warn: 0, fail: 0 } }), { tone: 'ok', label: 'Healthy', line: '30 pass · 0 warnings · 0 failed' })
  assert.equal(verdict({ counts: { pass: 28, warn: 1, fail: 0 } }).label, 'Healthy, with warnings')
  assert.equal(verdict({ counts: { pass: 28, warn: 1, fail: 0 } }).line, '28 pass · 1 warning · 0 failed')
  assert.deepEqual([verdict({ counts: { pass: 3, warn: 2, fail: 5 } }).tone, verdict({ counts: { pass: 3, warn: 2, fail: 5 } }).label], ['danger', 'Not healthy'])
  assert.equal(verdict(null).tone, 'neutral')
})

test('a bar knows the value, the target and when it is over', () => {
  const under = meter(876, 1000)
  assert.ok(under.fill < under.target && !under.over && under.target < 100)
  const over = meter(1240, 1000)
  assert.ok(over.over && over.fill > over.target && over.fill <= 100)
  assert.equal(meter(0, 1000).fill, 0)
  assert.equal(meter(500, 0).target, null)
  assert.equal(meter(undefined, undefined).fill, 0)
})

test('stage bars share one scale: the largest p95 is the full bar, the p50 sits inside its p95', () => {
  const bars = stageBars({ vad_stop: { p50: 200, p95: 200 }, llm_first_token: { p50: 260, p95: 400 }, mystery: { p50: null, p95: 50 } })
  assert.deepEqual(bars.map((b) => b.title), [STAGES.vad_stop, STAGES.llm_first_token, 'mystery'])
  assert.equal(bars[1].p95Pct, 100)
  assert.equal(bars[0].p95Pct, 50)
  assert.ok(bars.every((b) => b.p50Pct <= b.p95Pct))
  assert.equal(bars[2].p50Pct, 0)
  assert.deepEqual(stageBars(undefined), [])
})

test('the stages are the ones the harness writes, in the order of a turn', () => {
  assert.deepEqual(Object.keys(STAGES), ['vad_stop', 'turn_decision', 'stt_final', 'llm_first_token', 'tts_first_audio', 'network'])
  assert.deepEqual(RATES.map((r) => r[0]), ['false_interrupt_rate', 'missed_turn_rate', 'speaker_false_accept_rate'])
})

test('milliseconds and rates read like people say them', () => {
  assert.deepEqual([ms(875), ms(1240), ms(null), ms(undefined)], ['875 ms', '1.24 s', '–', '–'])
  assert.deepEqual([percent(0), percent(0.025), percent(0.0004), percent(null)], ['0.0 %', '2.5 %', '0.04 %', '–'])
})

test('the machine and the settings are listed only where the harness said them', () => {
  assert.deepEqual(machineRows({ gpu: 'NVIDIA GeForce RTX 3060', vram_gb: 12, ram_gb: 16, cpu: 'Example CPU' }),
    [['Graphics card', 'NVIDIA GeForce RTX 3060 · 12 GB'], ['Memory', '16 GB'], ['Processor', 'Example CPU']])
  assert.deepEqual(machineRows({ gpu: 'X' }), [['Graphics card', 'X']])
  assert.deepEqual(configRows({ vad_stop_s: 0.2, turn_analyzer: 'smart-turn-v3.2', stt_model: 'large-v3-turbo', stt_compute_type: 'float16', llm_model: 'm', tts_en: 'k', tts_de: 'p' }).map((r) => r[1]),
    ['0.2 s', 'smart-turn-v3.2', 'large-v3-turbo (float16)', 'm', 'k', 'p'])
  assert.deepEqual(configRows(undefined), [])
})
