#!/usr/bin/env node
/**
 * 从 BFF 的 OpenAPI 契约生成前端接口类型。
 * 契约是唯一事实来源：schemas/openapi.json 由 `npm run export:contract` 落地。
 * 禁止手写接口类型 —— 每次契约变更后重跑本脚本。
 *
 * 用法：
 *   1) 先起 BFF，然后： curl -s http://127.0.0.1:18081/openapi.json -o schemas/openapi.json
 *   2) npm run gen:api
 */
import { readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '..')
const schemaPath = resolve(root, 'schemas/openapi.json')

const spec = JSON.parse(readFileSync(schemaPath, 'utf8'))

mkdirSync(resolve(root, 'src/api'), { recursive: true })

const tmp = resolve(root, 'src/api/.schema.ts')
execFileSync(
  process.execPath,
  [
    resolve(root, 'node_modules/openapi-typescript/bin/cli.js'),
    schemaPath,
    '--output',
    tmp,
  ],
  { stdio: 'inherit' },
)

const header = `/* eslint-disable */
/**
 * 本文件由 scripts/gen-api-types.mjs 从 BFF OpenAPI 契约自动生成，请勿手改。
 * 契约来源：${schemaPath}
 */
export type { paths, components } from './.schema'

import type { components } from './.schema'

/** 统一错误体：{code, message, hint} —— hint 必填，写「下一步该干什么」 */
export type ApiErrorBody = {
  code: string
  message: string
  hint: string
}

/** e2e-status 的单块失败描述 */
export type PartialError = {
  block: string
  tool: string
  kind: string
  message: string
  hint: string
}

export type Schemas = components['schemas']
`

writeFileSync(resolve(root, 'src/api/schema.ts'), header, 'utf8')
console.log('generated src/api/schema.ts')