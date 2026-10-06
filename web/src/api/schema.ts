/* eslint-disable */
/**
 * 本文件由 scripts/gen-api-types.mjs 从 BFF OpenAPI 契约自动生成，请勿手改。
 * 契约来源：/Users/FYP/Documents/WorkSpace/askoda/web/schemas/openapi.json
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
