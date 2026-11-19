import { Badge, Button, Group, Stack, Text, ThemeIcon, Tooltip } from '@mantine/core'
import { IconClockExclamation, IconInbox } from '@tabler/icons-react'
import type { ComponentType, ReactNode } from 'react'
import type { EventOut } from '../api/client'
import { daysAgo, EVENT_STYLE, eventLabel, formatDate, useWords } from '../lib'

export function EventBadge({ type }: { type: EventOut['type'] }) {
  const style = EVENT_STYLE[type]
  const words = useWords()
  const Icon = style.icon
  return (
    <Badge variant="light" color={style.color} leftSection={<Icon size={12} />} styles={{ root: { flexShrink: 0 } }}>
      {eventLabel(type, words)}
    </Badge>
  )
}

const STALE_AFTER_DAYS = 14 // two weekly refresh cycles

function ageLabel(days: number): string {
  if (days === 0) return 'today'
  if (days === 1) return 'yesterday'
  return `${days} days ago`
}

/** "Last verified" with an explicit age; anything older than two refresh cycles is flagged. */
export function Freshness({ value }: { value: string | null | undefined }) {
  const days = daysAgo(value)
  const words = useWords()
  if (days === null) return <Text size="sm" c="dimmed">never</Text>
  const stale = days > STALE_AFTER_DAYS
  return (
    <Tooltip label={`Last checked on the ${words.org.toLowerCase()}'s website: ${formatDate(value)}`}>
      <Group gap={4} wrap="nowrap">
        {stale && <IconClockExclamation size={14} aria-label="stale" />}
        <Text size="sm" c={stale ? 'orange' : undefined}>
          {ageLabel(days)}
        </Text>
      </Group>
    </Tooltip>
  )
}

export function PageTitle({ title }: { title: string }) {
  return (
    <Text component="h1" fz={24} fw={700} m={0}>
      {title}
    </Text>
  )
}

/** The same look for every "nothing here" message: an icon, a title, and an optional next step. */
export function EmptyState({
  title,
  children,
  action,
  onAction,
  icon: Icon = IconInbox,
}: {
  title: string
  children?: ReactNode
  action?: string
  onAction?: () => void
  icon?: ComponentType<{ size?: number }>
}) {
  return (
    <Stack align="center" gap="xs" py="xl" ta="center">
      <ThemeIcon variant="light" size={48} radius="xl" color="gray">
        <Icon size={26} />
      </ThemeIcon>
      <Text fw={600}>{title}</Text>
      {children && (
        <Text size="sm" c="dimmed" maw={520}>
          {children}
        </Text>
      )}
      {action && onAction && (
        <Button variant="light" onClick={onAction} mt={4}>
          {action}
        </Button>
      )}
    </Stack>
  )
}
