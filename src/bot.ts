import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join } from 'node:path'

/**
 * Starts the Python model when a live session opens and stops it when the session ends, so that
 * pressing "Start trading" on the page is enough. The model is an ordinary client of /v1/stream;
 * this class only manages its process.
 */
/** One model the page can choose. `args` is empty for "no automatic trading". */
export interface BotModel {
  id: string
  label: string
  description: string
  args: string[]
  /** Extra environment for this model's process (merged over BotOptions.env). */
  env?: Record<string, string>
  /** Instruments it can trade. Other sessions run without a bot. */
  instruments: string[]
}

/** What app.ts needs: follow the session, report the choices, change the choice. */
export interface BotControl {
  sync: (session: SessionLike) => void
  stop: () => void
  info: () => BotInfo
  select: (id: string) => boolean
}

export interface BotInfo {
  enabled: boolean
  selected: string
  running: boolean
  models: Array<Pick<BotModel, 'id' | 'label' | 'description' | 'instruments'>>
}

export interface BotOptions {
  command: string
  models: BotModel[]
  selected: string
  cwd: string
  /** Where the bot reaches this server, passed to it as MODEL_API_URL. */
  url: string
  /** Extra environment for the bot process, e.g. the server's fee rates. */
  env?: Record<string, string>
  log?: (message: string) => void
  /** A bot that exits sooner than this after starting counts as a failed start. */
  quickExitMs?: number
  maxFailedStarts?: number
  retryDelayMs?: number
  now?: () => number
}

export interface SessionLike {
  status?: string
  instrument?: string
}

export class BotRunner implements BotControl {
  private child: ChildProcess | null = null
  private selected: string
  private lastSession: SessionLike = {}
  private startedAt = 0
  private lastExit = 0
  private failedStarts = 0

  constructor(private readonly options: BotOptions) {
    this.selected = options.models.some((m) => m.id === options.selected) ? options.selected : options.models[0]?.id ?? ''
  }

  get running(): boolean {
    return this.child !== null
  }

  private get model(): BotModel | undefined {
    return this.options.models.find((m) => m.id === this.selected)
  }

  info(): BotInfo {
    return {
      enabled: true,
      selected: this.selected,
      running: this.running,
      models: this.options.models.map(({ id, label, description, instruments }) => ({ id, label, description, instruments })),
    }
  }

  /** Choose the model for this and later sessions. A bot that is already trading is replaced at once. Returns false for an unknown id. */
  select(id: string): boolean {
    if (!this.options.models.some((m) => m.id === id)) return false
    if (id !== this.selected) {
      this.selected = id
      this.stop()
      this.sync(this.lastSession)
    }
    return true
  }

  /** Call whenever the session changes. Idempotent. */
  sync(session: SessionLike): void {
    this.lastSession = session
    const model = this.model
    const wanted = session.status === 'running' && !!model && model.args.length > 0 && model.instruments.includes(session.instrument ?? '')
    if (wanted) this.start()
    else this.stop()
  }

  start(): void {
    const model = this.model
    if (this.child || !model || model.args.length === 0) return
    const now = (this.options.now ?? Date.now)()
    const max = this.options.maxFailedStarts ?? 3
    if (this.failedStarts >= max) return
    if (this.lastExit && now - this.lastExit < (this.options.retryDelayMs ?? 5_000)) return
    const log = this.options.log ?? (() => {})
    const child = spawn(this.options.command, model.args, {
      cwd: this.options.cwd,
      env: { ...process.env, ...this.options.env, ...model.env, MODEL_API_URL: this.options.url, PYTHONUNBUFFERED: '1' },
      stdio: ['ignore', 'inherit', 'inherit'],
    })
    this.child = child
    this.startedAt = now
    log(`model bot '${model.id}' started (pid ${child.pid}): ${this.options.command} ${model.args.join(' ')}`)
    child.on('error', (error) => {
      log(`model bot could not start: ${error.message}`)
      if (this.child === child) this.exited(child)
    })
    child.on('exit', (code, signal) => {
      if (this.child !== child) return
      log(`model bot exited (${signal ?? code})`)
      this.exited(child)
    })
  }

  stop(): void {
    this.failedStarts = 0
    this.lastExit = 0
    const child = this.child
    if (!child) return
    this.child = null
    child.kill('SIGTERM')
    this.options.log?.(`model bot stopped (pid ${child.pid})`)
  }

  private exited(child: ChildProcess): void {
    if (this.child !== child) return
    const now = (this.options.now ?? Date.now)()
    this.child = null
    this.lastExit = now
    if (now - this.startedAt < (this.options.quickExitMs ?? 10_000)) {
      this.failedStarts += 1
      if (this.failedStarts >= (this.options.maxFailedStarts ?? 3)) {
        this.options.log?.('model bot keeps failing, not restarting until the next session')
      }
    } else {
      this.failedStarts = 0
    }
  }
}

/** `.venv` python when the repo has one, otherwise the system python. */
export function defaultBotCommand(root: string): string {
  const venv = [join(root, '.venv', 'bin', 'python'), join(root, '.venv', 'Scripts', 'python.exe')].find(existsSync)
  return process.env.MODEL_BOT_PYTHON ?? venv ?? (process.platform === 'win32' ? 'python' : 'python3')
}

/** The models the page offers. Each is `python -m model.live <kind>`; see model/live.py. */
export const BOT_MODELS: BotModel[] = [
  {
    id: 'small',
    label: 'Small neural network',
    description: 'A 929-parameter network on 11 order-book and trade features. Bitcoin only.',
    args: ['-m', 'model.live', 'session'],
    instruments: ['BTC-USDT'],
  },
  {
    id: 'transformer',
    label: 'Transformer',
    description: 'A transformer over the last 30 seconds of order-book ticks, trading short 5-second moves. Bitcoin only.',
    args: ['-m', 'model.live', 'transformer'],
    instruments: ['BTC-USDT'],
  },
  {
    id: 'network',
    label: 'Team network (forecasts returns)',
    description: "The team's network predicts the forward return and sends taker orders when it clears an entry level (BOT_STRATEGY=model). Bitcoin only.",
    args: ['-m', 'model.live'],
    env: { BOT_STRATEGY: 'model' },
    instruments: ['BTC-USDT'],
  },
  {
    id: 'hft',
    label: 'HFT order-book rule',
    description: "The team's rule bot: enters when the book leans one way, exits when it stops or after 5 seconds (BOT_STRATEGY=hft). Bitcoin only.",
    args: ['-m', 'model.live'],
    env: { BOT_STRATEGY: 'hft' },
    instruments: ['BTC-USDT'],
  },
  {
    id: 'none',
    label: 'None (I run my own bot)',
    description: 'The server starts nothing; connect your own bot to /v1/stream.',
    args: [],
    instruments: [],
  },
]
