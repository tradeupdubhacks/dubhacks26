import { execFileSync, spawn, type ChildProcess } from 'node:child_process'
import { cpSync, existsSync, mkdirSync, openSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { delimiter, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  Connection,
  Keypair,
  LAMPORTS_PER_SOL,
  PublicKey,
  SystemProgram,
  Transaction,
  sendAndConfirmTransaction,
} from '@solana/web3.js'
import {
  createMint,
  createTransferInstruction,
  getOrCreateAssociatedTokenAccount,
  mintTo,
  type Account,
} from '@solana/spl-token'
import type { Side } from './types.js'

export class ChainError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ChainError'
  }
}

/** One leg of a pool fill. Lots and cents are the chain token amounts. */
export interface SettlementLeg {
  side: Side
  lots: number
  quoteCents: number
}

export interface Chain {
  start(): Promise<void>
  settle(legs: SettlementLeg[]): Promise<string>
  snapshot(): { rpc: string; ready: boolean; pool: string | null }
  close(): Promise<void>
}

interface MintFile {
  base: string
  quote: string
}

/**
 * Settles live fills as SPL token transfers on a local Solana validator.
 * Training never calls this. The pool and the counterparty are dummy keys
 * held by the gateway.
 */
export class LocalSolana implements Chain {
  private readonly connection: Connection
  private readonly port: number
  private readonly pool: Keypair
  private readonly counterparty: Keypair
  private baseMint: PublicKey | null = null
  private quoteMint: PublicKey | null = null
  private poolBase: Account | null = null
  private poolQuote: Account | null = null
  private counterpartyBase: Account | null = null
  private counterpartyQuote: Account | null = null
  private ready = false
  private child: ChildProcess | null = null
  private starting: Promise<void> | null = null

  constructor(
    private readonly rpc: string,
    private readonly directory: string,
  ) {
    this.connection = new Connection(rpc, 'confirmed')
    const url = new URL(rpc)
    this.port = Number(url.port || (url.protocol === 'https:' ? 443 : 8899))
    mkdirSync(directory, { recursive: true })
    this.pool = loadKeypair(join(directory, 'pool.json'))
    this.counterparty = loadKeypair(join(directory, 'counterparty.json'))
  }

  snapshot(): { rpc: string; ready: boolean; pool: string | null } {
    return { rpc: this.rpc, ready: this.ready, pool: this.pool.publicKey.toBase58() }
  }

  start(): Promise<void> {
    if (this.ready) return Promise.resolve()
    if (!this.starting) {
      this.starting = this.bootstrap().catch((error: unknown) => {
        this.starting = null
        throw error
      })
    }
    return this.starting
  }

  async settle(legs: SettlementLeg[]): Promise<string> {
    await this.start()
    let base = 0
    let quote = 0
    for (const leg of legs) {
      const sign = leg.side === 'buy' ? 1 : -1
      base += sign * leg.lots
      quote += sign * leg.quoteCents
    }
    const tx = new Transaction()
    if (base > 0) tx.add(transfer(this.counterpartyBase!, this.poolBase!, this.counterparty.publicKey, base))
    if (base < 0) tx.add(transfer(this.poolBase!, this.counterpartyBase!, this.pool.publicKey, -base))
    if (quote > 0) tx.add(transfer(this.poolQuote!, this.counterpartyQuote!, this.pool.publicKey, quote))
    if (quote < 0) tx.add(transfer(this.counterpartyQuote!, this.poolQuote!, this.counterparty.publicKey, -quote))
    if (tx.instructions.length === 0) return 'empty'
    try {
      return await sendAndConfirmTransaction(this.connection, tx, [this.pool, this.counterparty], {
        commitment: 'confirmed',
      })
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Solana settlement failed.'
      throw new ChainError(message)
    }
  }

  async close(): Promise<void> {
    const socket = (this.connection as unknown as { _rpcWebSocket?: { removeAllListeners: () => void; close: () => void } })
      ._rpcWebSocket
    socket?.removeAllListeners()
    socket?.close()
    this.child?.kill()
    this.child = null
    this.ready = false
    this.starting = null
  }

  private async bootstrap(): Promise<void> {
    if (!(await this.reachable())) await this.launch()
    await this.fund(this.pool)
    await this.fund(this.counterparty)
    await this.ensureMints()
    this.ready = true
  }

  private async reachable(): Promise<boolean> {
    try {
      await this.connection.getVersion()
      return true
    } catch {
      return false
    }
  }

  private async launch(): Promise<void> {
    const bin = findValidator()
    if (!bin) {
      throw new ChainError(
        'Nothing is listening on the local Solana RPC, and solana-test-validator is not installed. Install the Agave CLI and restart.',
      )
    }
    const prepared = prepareLedger(this.directory)
    const args = ['--quiet', '--bind-address', '127.0.0.1', '--rpc-port', String(this.port), '--ledger', prepared.ledger]
    if (prepared.reset) args.unshift('--reset')
    const logPath = join(this.directory, 'validator.log')
    const log = openSync(logPath, 'a')
    this.child = spawn(bin, args, {
      stdio: ['ignore', log, log],
      env: { ...process.env, PATH: `${dirname(bin)}${delimiter}${process.env.PATH ?? ''}` },
    })
    for (let attempt = 0; attempt < 60; attempt += 1) {
      if (await this.reachable()) {
        return
      }
      if (this.child.exitCode !== null && this.child.exitCode !== 0) break
      await delay(500)
    }
    throw new ChainError(`The local validator exited before ${this.rpc} accepted requests. See .local-solana/validator.log.`)
  }

  private async fund(owner: Keypair): Promise<void> {
    const balance = await this.connection.getBalance(owner.publicKey)
    if (balance >= LAMPORTS_PER_SOL) return
    const signature = await this.connection.requestAirdrop(owner.publicKey, 2 * LAMPORTS_PER_SOL)
    await this.connection.confirmTransaction(signature, 'confirmed')
  }

  private async ensureMints(): Promise<void> {
    const saved = readMints(join(this.directory, 'mints.json'))
    if (saved) {
      const baseInfo = await this.connection.getAccountInfo(new PublicKey(saved.base))
      const quoteInfo = await this.connection.getAccountInfo(new PublicKey(saved.quote))
      if (baseInfo && quoteInfo) {
        this.baseMint = new PublicKey(saved.base)
        this.quoteMint = new PublicKey(saved.quote)
      }
    }
    if (!this.baseMint || !this.quoteMint) {
      this.baseMint = await createMint(this.connection, this.pool, this.pool.publicKey, null, 0)
      this.quoteMint = await createMint(this.connection, this.pool, this.pool.publicKey, null, 0)
      writeFileSync(
        join(this.directory, 'mints.json'),
        JSON.stringify({ base: this.baseMint.toBase58(), quote: this.quoteMint.toBase58() }),
      )
    }
    this.poolBase = await this.account(this.baseMint, this.pool)
    this.poolQuote = await this.account(this.quoteMint, this.pool)
    this.counterpartyBase = await this.account(this.baseMint, this.counterparty)
    this.counterpartyQuote = await this.account(this.quoteMint, this.counterparty)
    const supply = 1_000_000_000
    await this.topUp(this.baseMint, this.poolBase, supply)
    await this.topUp(this.quoteMint, this.poolQuote, supply)
    await this.topUp(this.baseMint, this.counterpartyBase, supply)
    await this.topUp(this.quoteMint, this.counterpartyQuote, supply)
  }

  private async account(mint: PublicKey, owner: Keypair): Promise<Account> {
    return getOrCreateAssociatedTokenAccount(this.connection, this.pool, mint, owner.publicKey)
  }

  private async topUp(mint: PublicKey, account: Account, target: number): Promise<void> {
    if (account.amount >= BigInt(target)) return
    await mintTo(this.connection, this.pool, mint, account.address, this.pool, target)
    account.amount = BigInt(target)
  }
}

function transfer(source: Account, destination: Account, owner: PublicKey, amount: number) {
  return createTransferInstruction(source.address, destination.address, owner, amount)
}

function loadKeypair(path: string): Keypair {
  if (existsSync(path)) {
    const secret = Uint8Array.from(JSON.parse(readFileSync(path, 'utf8')) as number[])
    return Keypair.fromSecretKey(secret)
  }
  const key = Keypair.generate()
  writeFileSync(path, JSON.stringify(Array.from(key.secretKey)))
  return key
}

function readFaucet(path: string): Keypair | null {
  if (!existsSync(path)) return null
  return Keypair.fromSecretKey(Uint8Array.from(JSON.parse(readFileSync(path, 'utf8')) as number[]))
}

function readMints(path: string): MintFile | null {
  if (!existsSync(path)) return null
  const parsed = JSON.parse(readFileSync(path, 'utf8')) as MintFile
  if (typeof parsed.base !== 'string' || typeof parsed.quote !== 'string') return null
  return parsed
}

const freshLedger = join(dirname(fileURLToPath(import.meta.url)), '../chain/fresh-ledger')

/**
 * Agave 4.3.1's new-ledger check opens a directory with File::open. On Windows
 * that returns os error 5, so --reset never finishes. A ledger that already
 * contains vote-account-keypair.json skips that check.
 */
function prepareLedger(directory: string): { ledger: string; reset: boolean } {
  const ledger = join(directory, 'ledger')
  if (process.platform !== 'win32') return { ledger, reset: true }
  const vote = join(freshLedger, 'vote-account-keypair.json')
  if (!existsSync(vote)) {
    throw new ChainError('The Windows ledger template at chain/fresh-ledger is missing. Reinstall the server checkout.')
  }
  rmSync(ledger, { recursive: true, force: true })
  cpSync(freshLedger, ledger, { recursive: true })
  rmSync(join(ledger, 'rocksdb', 'LOCK'), { force: true })
  return { ledger, reset: false }
}

function findValidator(): string | null {
  const names = process.platform === 'win32'
    ? ['solana-test-validator.exe', 'solana-test-validator']
    : ['solana-test-validator']
  const candidates = [
    process.env.SOLANA_TEST_VALIDATOR,
    ...names.map((name) => join(homedir(), '.local/share/solana/install/active_release/bin', name)),
  ].filter((item): item is string => Boolean(item))
  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate
  }
  const locator = process.platform === 'win32' ? 'where.exe' : 'which'
  for (const name of names) {
    try {
      const found = execFileSync(locator, [name], { encoding: 'utf8' })
        .split(/\r?\n/)
        .map((line) => line.trim())
        .find(Boolean)
      if (found && existsSync(found)) return found
    } catch {
      // try the next name
    }
  }
  return null
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}
