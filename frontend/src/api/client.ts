import createClient from 'openapi-fetch'
import type { components, paths } from './schema'

// Types are generated from the FastAPI OpenAPI schema (`make types`), so the
// frontend and backend can't silently disagree about a field.
export type Schemas = components['schemas']
export type PersonSummary = Schemas['PersonSummary']
export type PersonDetail = Schemas['PersonDetail']
export type EventOut = Schemas['EventOut']
export type BriefOut = Schemas['BriefOut']
export type OrganisationOut = Schemas['OrganisationOut']
export type Meta = Schemas['Meta']
export type Me = Schemas['Me']

export const api = createClient<paths>({ baseUrl: '', credentials: 'same-origin' })

export class ApiError extends Error {
  readonly status: number
  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

type Result<T> = { data?: T; error?: unknown; response: Response }

type ValidationIssue = { loc?: (string | number)[]; msg?: string }

/** FastAPI's `detail` is a string, or (for a 422) a list of validation issues with where they are. */
function describe(detail: unknown): string | null {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return (detail as ValidationIssue[])
      .map((issue) => {
        const where = (issue.loc ?? []).slice(1).join('.')
        return where ? `${where}: ${issue.msg}` : (issue.msg ?? '')
      })
      .join('; ')
  }
  return null
}

/** Turn an openapi-fetch result into data or a thrown ApiError carrying the server's `detail`. */
export async function unwrap<T>(request: Promise<Result<T>>): Promise<T> {
  const { data, error, response } = await request
  if (!response.ok || error !== undefined) {
    const detail = (error as { detail?: unknown } | undefined)?.detail
    throw new ApiError(describe(detail) ?? (response.statusText || 'Request failed'), response.status)
  }
  return data as T
}
