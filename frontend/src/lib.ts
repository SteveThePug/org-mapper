import {
  IconArrowsExchange,
  IconBriefcase,
  IconDoorEnter,
  IconDoorExit,
  IconMapPin,
  IconQuestionMark,
  IconTrendingUp,
  IconUsersGroup,
} from '@tabler/icons-react'
import { useQuery } from '@tanstack/react-query'
import type { ComponentType } from 'react'
import { useSearchParams } from 'react-router'
import { api, ApiError, unwrap, type EventOut, type Me, type Schemas } from './api/client'

export function useMe() {
  return useQuery({
    queryKey: ['me'],
    queryFn: async (): Promise<Me | null> => {
      try {
        return await unwrap(api.GET('/api/auth/me'))
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) return null
        throw error
      }
    },
  })
}

export function useMeta() {
  return useQuery({ queryKey: ['meta'], queryFn: () => unwrap(api.GET('/api/meta')), staleTime: 5 * 60_000 })
}

export type Vocabulary = Schemas['Vocabulary']

// Used until /api/meta has loaded, so labels never flash empty.
const DEFAULT_WORDS: Vocabulary = {
  app_name: 'Radar',
  tagline: '',
  person: 'Person',
  people: 'People',
  org: 'Organisation',
  orgs: 'Organisations',
  group: 'Group',
  groups: 'Groups',
  location: 'Location',
  locations: 'Locations',
}

/** The words the domain profile uses for people, organisations and groups ("Partners", "Clubs"...). */
export function useWords(): Vocabulary {
  return useMeta().data?.vocabulary ?? DEFAULT_WORDS
}

type EventType = EventOut['type']

// Every event type has an icon and a label as well as a colour, so meaning never relies on colour alone.
export const EVENT_STYLE: Record<EventType, { label: string; color: string; icon: ComponentType<{ size?: number }> }> = {
  joined: { label: 'Joined', color: 'teal', icon: IconDoorEnter },
  left: { label: 'Left', color: 'red', icon: IconDoorExit },
  moved: { label: 'Move', color: 'indigo', icon: IconArrowsExchange },
  possible_move: { label: 'Possible move', color: 'yellow', icon: IconQuestionMark },
  promoted: { label: 'Promoted', color: 'grape', icon: IconTrendingUp },
  title_changed: { label: 'Title change', color: 'gray', icon: IconBriefcase },
  office_changed: { label: 'Office change', color: 'cyan', icon: IconMapPin },
  group_changed: { label: 'Group change', color: 'blue', icon: IconBriefcase },
  lift_out: { label: 'Team lift-out', color: 'orange', icon: IconUsersGroup },
}

/** An event type's label in the domain's words ("Office change", "Position change"). */
export function eventLabel(type: EventType, words: Vocabulary): string {
  if (type === 'office_changed') return `${words.location} change`
  if (type === 'group_changed') return `${words.group} change`
  return EVENT_STYLE[type].label
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return '-'
  return new Date(value).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' })
}

export function daysAgo(value: string | null | undefined): number | null {
  if (!value) return null
  return Math.floor((Date.now() - new Date(value).getTime()) / 86_400_000)
}

export function money(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined) return '-'
  if (value === 0) return '$0'
  return `$${value.toFixed(value < 0.01 ? digits : 2)}`
}

/**
 * Page filters kept in the URL (?organisation=x&period=90) instead of in component state, so the Back
 * button, a page refresh and a copied link all keep them. Values are strings; '' means "not set".
 * A value equal to its default is left out of the URL to keep links short.
 */
export function useUrlFilters<K extends string>(defaults: Record<K, string>) {
  const [params, setParams] = useSearchParams()
  const keys = Object.keys(defaults) as K[]
  const values = Object.fromEntries(keys.map((key) => [key, params.get(key) ?? defaults[key]])) as Record<K, string>
  // Several filters can change in one go (e.g. a new organisation also goes back to page 1).
  const update = (patch: Partial<Record<K, string | null>>) =>
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous)
        for (const [key, value] of Object.entries(patch) as [K, string | null][]) {
          if (value === null || value === defaults[key]) next.delete(key)
          else next.set(key, value)
        }
        return next
      },
      // Replace rather than push: typing in a search box shouldn't add a history entry per letter.
      { replace: true },
    )
  const isFiltered = keys.some((key) => params.has(key))
  const clear = () => setParams(new URLSearchParams(), { replace: true })
  return { values, update, isFiltered, clear }
}
