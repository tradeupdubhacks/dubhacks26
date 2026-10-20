import assert from 'node:assert/strict'
import { describe, it } from 'node:test'
import { BotRunner, type BotModel } from '../src/bot.js'
import { buildApp } from '../src/app.js'

const sleeper = ['-e', 'setInterval(() => {}, 1000)']
const crasher = ['-e', 'process.exit(1)']
const model = (id: string, args: string[], instruments = ['BTC-USDT']): BotModel => ({ id, label: id, description: `${id} model`, args, instruments })

function runner(models: BotModel[], extra?: object, selected = models[0]!.id) {
  const logs: string[] = []
  const bot = new BotRunner({
    command: process.execPath, models, selected, cwd: process.cwd(), url: 'http://127.0.0.1:3000',
    log: (m) => logs.push(m), ...extra,
  })
  return { bot, logs }
}
const until = async (check: () => boolean) => {
  for (let i = 0; i < 100 && !check(); i += 1) await new Promise((r) => setTimeout(r, 20))
}

describe('model bot process', () => {
  it('starts once for a running BTC session and stops when it ends', async () => {
    const { bot } = runner([model('a', sleeper)])
    bot.sync({ status: 'running', instrument: 'BTC-USDT' })
    bot.sync({ status: 'running', instrument: 'BTC-USDT' })
    assert.equal(bot.running, true)
    bot.sync({ status: 'halted', instrument: 'BTC-USDT' })
    assert.equal(bot.running, false)
    bot.sync({ status: 'running', instrument: 'BTC-USDT' })
    assert.equal(bot.running, true)
    bot.sync({ status: 'idle' })
    assert.equal(bot.running, false)
  })

  it('does not start for an instrument the selected model cannot trade', () => {
    const { bot } = runner([model('a', sleeper)])
    bot.sync({ status: 'running', instrument: 'SOL-USD' })
    assert.equal(bot.running, false)
  })

  it('passes the server address and extra environment to the bot', async () => {
    const probe = ['-e', "process.exit(process.env.MODEL_API_URL === 'http://127.0.0.1:3000' && process.env.TAKER_BPS === '0' ? 0 : 7)"]
    const { bot, logs } = runner([model('a', probe)], { env: { TAKER_BPS: '0' } })
    bot.sync({ status: 'running', instrument: 'BTC-USDT' })
    await until(() => logs.some((m) => m.includes('exited')))
    assert.ok(logs.some((m) => m.includes('exited (0)')), logs.join('|'))
  })

  it("gives each model its own environment (how BOT_STRATEGY picks the team's bots)", async () => {
    const probe = ['-e', "process.exit(process.env.BOT_STRATEGY === 'hft' && process.env.SHARED === '1' ? 0 : 9)"]
    const hft = { ...model('hft', probe), env: { BOT_STRATEGY: 'hft' } }
    const { bot, logs } = runner([hft], { env: { SHARED: '1' } })
    bot.sync({ status: 'running', instrument: 'BTC-USDT' })
    await until(() => logs.some((m) => m.includes('exited')))
    assert.ok(logs.some((m) => m.includes('exited (0)')), logs.join('|'))
  })

  it('gives up on a bot that keeps crashing until the next session', async () => {
    const { bot, logs } = runner([model('a', crasher)], { retryDelayMs: 0 })
    for (let i = 0; i < 6; i += 1) {
      bot.sync({ status: 'running', instrument: 'BTC-USDT' })
      await until(() => !bot.running)
    }
    assert.equal(logs.filter((m) => m.includes('started')).length, 3)
    assert.ok(logs.some((m) => m.includes('keeps failing')))
    bot.sync({ status: 'idle' })
    bot.sync({ status: 'running', instrument: 'BTC-USDT' })
    assert.equal(logs.filter((m) => m.includes('started')).length, 4)
    bot.stop()
  })

  it('lists the models, switches the model mid-session and starts nothing for "none"', async () => {
    const { bot, logs } = runner([model('a', sleeper), model('b', sleeper), model('none', [], [])])
    assert.deepEqual(bot.info().models.map((m) => m.id), ['a', 'b', 'none'])
    assert.equal(bot.info().selected, 'a')
    assert.equal(bot.select('nope'), false)
    bot.sync({ status: 'running', instrument: 'BTC-USDT' })
    assert.equal(bot.running, true)
    assert.equal(bot.select('b'), true)                      // replaces the running bot
    assert.equal(bot.running, true)
    assert.equal(bot.info().selected, 'b')
    assert.ok(logs.some((m) => m.includes("'b' started")), logs.join('|'))
    assert.equal(bot.select('none'), true)
    assert.equal(bot.running, false)
    bot.sync({ status: 'running', instrument: 'BTC-USDT' })
    assert.equal(bot.running, false)
    bot.stop()
  })

  it('is driven by the app as sessions open and stop, and exposes the choice over HTTP', async () => {
    const seen: string[] = []
    let selected = 'a'
    const bot = {
      // The app reports every live session at once (several people can trade).
      sync: (sessions: Array<{ status?: string; instrument?: string }> | { status?: string; instrument?: string }) => {
        for (const s of Array.isArray(sessions) ? sessions : [sessions]) seen.push(`${s.status}:${s.instrument ?? ''}`)
      },
      stop: () => seen.push('closed'),
      info: () => ({ enabled: true, selected, running: false, models: [{ id: 'a', label: 'A', description: '', instruments: ['BTC-USDT'] }, { id: 'b', label: 'B', description: '', instruments: ['BTC-USDT'] }] }),
      select: (id: string) => (['a', 'b'].includes(id) ? ((selected = id), true) : false),
    }
    const built = await buildApp({ clock: false, bot })
    assert.deepEqual((await built.app.inject({ method: 'GET', url: '/v1/bot' })).json().models.map((m: { id: string }) => m.id), ['a', 'b'])
    const chosen = await built.app.inject({ method: 'POST', url: '/v1/bot', payload: { model: 'b' } })
    assert.equal(chosen.statusCode, 200)
    assert.equal(chosen.json().selected, 'b')
    const bad = await built.app.inject({ method: 'POST', url: '/v1/bot', payload: { model: 'zzz' } })
    assert.equal(bad.statusCode, 400)
    assert.equal(bad.json().error.code, 'unknown_model')
    const opened = await built.app.inject({ method: 'POST', url: '/v1/session', payload: { depositCents: 500, instrument: 'SOL-USD', floorCents: 250, ceilingCents: 800 } })
    assert.equal(opened.statusCode, 201)
    await built.app.inject({ method: 'POST', url: '/v1/stop' })
    await built.close()
    assert.ok(seen.includes('running:SOL-USD'), seen.join(','))
    assert.ok(seen.includes('halted:SOL-USD'), seen.join(','))
    assert.equal(seen.at(-1), 'closed')
  })

  it('runs one bot per model that has a running session, for several sessions at once', async () => {
    const { bot, logs } = runner([model('a', sleeper), model('b', sleeper), model('none', [], [])])
    const a1 = { status: 'running', instrument: 'BTC-USDT', strategy: 'a' }
    const a2 = { status: 'running', instrument: 'BTC-USDT', strategy: 'a' }
    const b1 = { status: 'running', instrument: 'BTC-USDT', strategy: 'b' }
    bot.sync([a1, a2, b1])
    assert.deepEqual(bot.info().runningModels?.sort(), ['a', 'b'])
    assert.equal(logs.filter((m) => m.includes("'a' started")).length, 1)   // one process for both 'a' sessions
    bot.sync([a1, a2, { ...b1, status: 'halted' }])
    assert.deepEqual(bot.info().runningModels, ['a'])
    bot.sync([{ ...a1, status: 'halted' }, a2])
    assert.deepEqual(bot.info().runningModels, ['a'])                      // a2 still needs it
    assert.equal(bot.select('b'), true)                                     // a session that named 'a' keeps it
    assert.deepEqual(bot.info().runningModels, ['a'])
    bot.sync([])
    assert.equal(bot.running, false)
    bot.stop()
  })

  it('tells the launcher about every session, each with its own model', async () => {
    const calls: Array<Array<{ sessionId?: string; strategy?: string; status?: string }>> = []
    const bot = {
      sync: (sessions: unknown) => { calls.push(sessions as Array<{ sessionId?: string; strategy?: string; status?: string }>) },
      stop: () => {},
      info: () => ({ enabled: true, selected: 'transformer', running: false, models: [] }),
      select: () => true,
    }
    const built = await buildApp({ clock: false, bot })
    const open = (payload: object) => built.app.inject({ method: 'POST', url: '/v1/session', payload: { depositCents: 500, instrument: 'SOL-USD', floorCents: 250, ceilingCents: 800, ...payload } })
    const first = (await open({ strategy: 'hft' })).json()
    const second = (await open({})).json()                                     // names no model: gets the selected one
    assert.equal(first.strategy, 'hft')
    assert.equal(second.strategy, 'transformer')
    const last = calls.at(-1)!
    assert.deepEqual(last.map((s) => [s.sessionId, s.strategy, s.status]), [[first.sessionId, 'hft', 'running'], [second.sessionId, 'transformer', 'running']])
    await built.close()
  })

  it('reports a disabled bot cleanly when the server runs without one', async () => {
    const built = await buildApp({ clock: false })
    assert.equal((await built.app.inject({ method: 'GET', url: '/v1/bot' })).json().enabled, false)
    assert.equal((await built.app.inject({ method: 'POST', url: '/v1/bot', payload: { model: 'a' } })).statusCode, 409)
    await built.close()
  })
})
