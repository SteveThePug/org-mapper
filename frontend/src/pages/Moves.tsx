import {
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Group,
  Input,
  List,
  Loader,
  MultiSelect,
  Paper,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Text,
  Title,
  Tooltip,
  UnstyledButton,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconExternalLink, IconQuestionMark, IconSparkles } from '@tabler/icons-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router'
import { api, unwrap, type EventOut } from '../api/client'
import { EmptyState, EventBadge, PageTitle } from '../components/common'
import { daysAgo, EVENT_STYLE, eventLabel, formatDate, useMeta, useUrlFilters, useWords } from '../lib'

const PERIODS = [
  { value: '7', label: '7 days' },
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days' },
  { value: '365', label: '12 months' },
]
const HEADLINE_TYPES = ['joined', 'left', 'moved', 'promoted'] as const

// The feed is split under headings by age, so "how recent?" is answered at a glance.
const AGE_GROUPS = [
  { upTo: 7, label: 'Last 7 days' },
  { upTo: 30, label: '8 to 30 days ago' },
  { upTo: 90, label: '1 to 3 months ago' },
  { upTo: Infinity, label: 'Older' },
]

function byAge(events: EventOut[]) {
  return AGE_GROUPS.map((group, i) => {
    const from = i === 0 ? -Infinity : AGE_GROUPS[i - 1].upTo
    const items = events.filter((e) => {
      const days = daysAgo(e.detected_at) ?? 0
      return days >= from && days < group.upTo
    })
    return { label: group.label, items }
  }).filter((group) => group.items.length > 0)
}

// A possible move nobody has confirmed or dismissed yet: it needs a person to look at it.
const needsReview = (e: EventOut) => e.type === 'possible_move' && e.status === 'auto'

export function MovesPage() {
  const meta = useMeta()
  const words = useWords()
  // types is a comma-separated list in the URL, e.g. ?types=joined,left
  const { values, update } = useUrlFilters({ period: '30', organisation: '', types: '' })
  const types = values.types ? values.types.split(',') : []
  const setTypes = (next: string[]) => update({ types: next.join(',') })

  // Fetch every type and filter on the page, so the headline counts stay right while a type filter is on.
  const events = useQuery({
    queryKey: ['events', values.period, values.organisation],
    queryFn: () =>
      unwrap(
        api.GET('/api/events', {
          params: { query: { since_days: Number(values.period), organisation: values.organisation || null, segment: 'all', limit: 500 } },
        }),
      ),
  })
  const digest = useMutation({
    mutationFn: () => unwrap(api.POST('/api/events/digest', { body: { since_days: Number(values.period) } })),
    onError: (error) => notifications.show({ color: 'orange', title: 'AI summary unavailable', message: error.message }),
  })

  const all = events.data ?? []
  const toReview = all.filter(needsReview)
  const shown = all.filter((e) => !needsReview(e) && (types.length === 0 || types.includes(e.type)))
  const counts = Object.fromEntries(HEADLINE_TYPES.map((t) => [t, all.filter((e) => e.type === t).length]))
  const periodLabel = PERIODS.find((p) => p.value === values.period)?.label ?? `${values.period} days`

  return (
    <Stack>
      <PageTitle title="Latest moves" />
      <Group align="end" wrap="wrap">
        <Input.Wrapper label="Period">
          <div>
            <SegmentedControl data={PERIODS} value={values.period} onChange={(period) => update({ period })} />
          </div>
        </Input.Wrapper>
        <Select
          label={words.org}
          placeholder={`All ${words.orgs.toLowerCase()}`}
          clearable
          searchable
          data={meta.data?.organisations.map((f) => ({ value: f.id, label: f.name })) ?? []}
          value={values.organisation || null}
          onChange={(organisation) => update({ organisation })}
          w={240}
        />
        <MultiSelect
          label="Type of change"
          placeholder={types.length ? undefined : 'All types'}
          clearable
          data={(Object.keys(EVENT_STYLE) as EventOut['type'][]).map((value) => ({ value, label: eventLabel(value, words) }))}
          value={types}
          onChange={setTypes}
          w={300}
        />
        <Button variant="light" leftSection={<IconSparkles size={16} />} loading={digest.isPending} onClick={() => digest.mutate()}>
          Summarise with AI
        </Button>
      </Group>

      {digest.data && (
        <Alert variant="light" color="indigo" title={digest.data.digest.headline} withCloseButton onClose={() => digest.reset()}>
          <List size="sm" spacing={2}>
            {(digest.data.digest.bullets ?? []).map((bullet) => (
              <List.Item key={bullet}>{bullet}</List.Item>
            ))}
          </List>
          <Text size="xs" c="dimmed" mt="xs">
            AI summary of {digest.data.event_count} changes · {digest.data.model}
          </Text>
        </Alert>
      )}

      {/* The tiles double as quick filters: click one to show only that type, click again to show all. */}
      <SimpleGrid cols={{ base: 2, sm: 4 }}>
        {HEADLINE_TYPES.map((type) => {
          const active = types.length === 1 && types[0] === type
          const Icon = EVENT_STYLE[type].icon
          return (
            <Tooltip key={type} label={active ? 'Click to show all types again' : `Show only "${eventLabel(type, words)}"`}>
              <UnstyledButton onClick={() => setTypes(active ? [] : [type])} aria-pressed={active}>
                <Paper
                  withBorder
                  p="md"
                  style={active ? { borderColor: `var(--mantine-color-${EVENT_STYLE[type].color}-6)`, borderWidth: 2 } : undefined}
                >
                  <Group gap={6} c={EVENT_STYLE[type].color}>
                    <Icon size={14} />
                    <Text size="xs" tt="uppercase" fw={600}>
                      {eventLabel(type, words)}
                    </Text>
                  </Group>
                  <Text fz={28} fw={700}>
                    {counts[type]}
                  </Text>
                  <Text size="xs" c="dimmed">
                    in the last {periodLabel}
                  </Text>
                </Paper>
              </UnstyledButton>
            </Tooltip>
          )
        })}
      </SimpleGrid>

      {toReview.length > 0 && <ReviewCard events={toReview} />}

      {events.isPending && <Loader />}
      {events.error && <Alert color="red">{events.error.message}</Alert>}
      {events.data && shown.length === 0 && (
        <Paper withBorder>
          <EmptyState
            title={types.length ? 'Nothing of that type in this period' : `No changes in the last ${periodLabel}`}
            action={values.period !== '365' ? 'Show the last 12 months' : types.length ? 'Show all types' : undefined}
            onAction={() => (values.period !== '365' ? update({ period: '365' }) : setTypes([]))}
          />
        </Paper>
      )}
      {byAge(shown).map((group) => (
        <AgeGroup key={group.label} label={group.label} events={group.items} />
      ))}
    </Stack>
  )
}

// A long period can hold hundreds of moves; each age group shows this many until asked for more.
const GROUP_PREVIEW = 20

function AgeGroup({ label, events }: { label: string; events: EventOut[] }) {
  const [all, setAll] = useState(false)
  const visible = all ? events : events.slice(0, GROUP_PREVIEW)
  return (
    <Stack gap="xs">
      <Title order={6} c="dimmed" tt="uppercase" mt="xs">
        {label} · {events.length}
      </Title>
      {visible.map((event) => (
        <EventRow key={event.id} event={event} />
      ))}
      {events.length > GROUP_PREVIEW && (
        <Button variant="subtle" size="xs" onClick={() => setAll(!all)} style={{ alignSelf: 'center' }}>
          {all ? `Show the first ${GROUP_PREVIEW} only` : `Show all ${events.length}`}
        </Button>
      )}
    </Stack>
  )
}

/** Uncertain matches pulled to the top, because they need a person's yes/no before they count. */
function ReviewCard({ events }: { events: EventOut[] }) {
  return (
    <Card withBorder padding="md" style={{ borderColor: 'var(--mantine-color-yellow-6)' }}>
      <Group gap={6} mb="xs">
        <IconQuestionMark size={18} color="var(--mantine-color-yellow-7)" />
        <Text fw={700}>
          {events.length === 1 ? '1 possible move needs you to check it' : `${events.length} possible moves need you to check them`}
        </Text>
      </Group>
      <Stack gap="xs">
        {events.map((event) => (
          <EventRow key={event.id} event={event} />
        ))}
      </Stack>
    </Card>
  )
}

function EventRow({ event }: { event: EventOut }) {
  const queryClient = useQueryClient()
  const review = useMutation({
    mutationFn: (action: 'confirm' | 'dismiss') =>
      unwrap(api.POST('/api/events/{event_id}/review', { params: { path: { event_id: event.id } }, body: { action } })),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['events'] }),
    onError: (error) => notifications.show({ color: 'red', title: 'Review not saved', message: error.message }),
  })
  return (
    <Card withBorder padding="sm">
      <Group justify="space-between" wrap="wrap" align="start" gap="xs">
        <Group gap="sm" wrap="nowrap" align="start">
          <EventBadge type={event.type} />
          <div>
            <Text size="sm">
              <Anchor component={Link} to={`/people/${event.person_id}`} fw={600}>
                {event.person_name}
              </Anchor>{' '}
              - {event.summary.replace(`${event.person_name} `, '')}
            </Text>
            <Group gap="xs" mt={2}>
              <Text size="xs" c="dimmed">
                {formatDate(event.detected_at)} ·{' '}
                <Anchor component={Link} to={`/people?organisation=${event.org_id}`} size="xs" c="dimmed" underline="hover">
                  {event.org_name}
                </Anchor>
                {event.offices.length ? ` · ${event.offices.join(', ')}` : ''}
              </Text>
              {event.evidence_url && (
                <Anchor href={event.evidence_url} target="_blank" rel="noreferrer" size="xs">
                  {event.source === 'archive' && event.type === 'left' ? 'old copy of their page' : 'where we saw this'}{' '}
                  <IconExternalLink size={11} />
                </Anchor>
              )}
              {event.status === 'confirmed' && (
                <Badge size="xs" variant="outline">
                  checked by a person
                </Badge>
              )}
              {event.source === 'archive' && (
                <Tooltip
                  multiline
                  w={300}
                  label="Worked out by comparing today's website with an old saved copy (Internet Archive), so the exact date isn't known."
                >
                  <Badge size="xs" variant="outline" color="gray">
                    date approximate
                  </Badge>
                </Tooltip>
              )}
              {event.simulated && (
                <Tooltip label="Test data, not a real change">
                  <Badge size="xs" color="gray">
                    example
                  </Badge>
                </Tooltip>
              )}
            </Group>
          </div>
        </Group>
        {needsReview(event) && (
          <Group gap={6} wrap="nowrap">
            <Button size="xs" variant="light" loading={review.isPending && review.variables === 'confirm'} onClick={() => review.mutate('confirm')}>
              Yes, same person
            </Button>
            <Button
              size="xs"
              variant="subtle"
              color="gray"
              loading={review.isPending && review.variables === 'dismiss'}
              onClick={() => review.mutate('dismiss')}
            >
              No, different people
            </Button>
          </Group>
        )}
      </Group>
    </Card>
  )
}
