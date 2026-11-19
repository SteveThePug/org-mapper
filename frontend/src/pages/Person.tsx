import {
  ActionIcon,
  Alert,
  Anchor,
  Badge,
  Button,
  Card,
  Grid,
  Group,
  List,
  Loader,
  Stack,
  Text,
  Timeline,
  Title,
  Tooltip,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import {
  IconAlertTriangle,
  IconArrowLeft,
  IconExternalLink,
  IconInfoCircle,
  IconNotes,
  IconRefresh,
  IconSparkles,
  IconThumbDown,
  IconThumbUp,
} from '@tabler/icons-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { Link, useLocation, useNavigate, useParams } from 'react-router'
import { api, unwrap, type BriefOut, type PersonDetail, type Schemas } from '../api/client'
import { EventBadge, Freshness } from '../components/common'
import { NotesPanel } from '../components/Notes'
import { formatDate, money, useMeta, useWords } from '../lib'

export function PersonPage() {
  const { id = '' } = useParams()
  const person = useQuery({
    queryKey: ['person', id],
    queryFn: () => unwrap(api.GET('/api/people/{person_id}', { params: { path: { person_id: id } } })),
  })
  if (person.isPending) return <Loader />
  if (person.error) return <Alert color="red">{person.error.message}</Alert>
  const p = person.data

  return (
    <Stack>
      <BackLink />
      <Header person={p} />
      <Grid gap="md">
        {/* On a phone the short facts come first and the (long) brief after them. */}
        <Grid.Col span={{ base: 12, lg: 7 }} order={{ base: 2, lg: 1 }}>
          <BriefCard person={p} />
        </Grid.Col>
        <Grid.Col span={{ base: 12, lg: 5 }} order={{ base: 1, lg: 2 }}>
          <Stack>
            <FactsCard person={p} />
            <SignalsCard signals={p.signals} />
            <NotesCard person={p} />
          </Stack>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 6 }} order={3}>
          <CareerCard person={p} />
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 6 }} order={4}>
          <ChangesCard events={p.events} />
        </Grid.Col>
      </Grid>
    </Stack>
  )
}

/** Back to wherever the user came from (a filtered list keeps its filters); the People page if they arrived by link. */
function BackLink() {
  const navigate = useNavigate()
  const location = useLocation()
  const words = useWords()
  // react-router marks the first page of a visit with key "default": there is nothing in the app to go back to.
  const canGoBack = location.key !== 'default'
  return (
    <Anchor
      component={Link}
      to="/people"
      size="sm"
      onClick={(e) => {
        if (canGoBack) {
          e.preventDefault()
          navigate(-1)
        }
      }}
    >
      <IconArrowLeft size={14} style={{ verticalAlign: -2 }} /> {canGoBack ? 'Back' : `All ${words.people.toLowerCase()}`}
    </Anchor>
  )
}

function Header({ person }: { person: PersonDetail }) {
  const words = useWords()
  const refresh = useMutation({
    mutationFn: () => unwrap(api.POST('/api/people/{person_id}/refresh', { params: { path: { person_id: person.id } } })),
    onSuccess: () =>
      notifications.show({ title: 'Checking again', message: 'We\'ll re-read their page shortly. Reload in a minute or two to see any changes.' }),
    onError: (error) => notifications.show({ color: 'red', title: 'Couldn\'t queue a re-check', message: error.message }),
  })
  return (
    <Group justify="space-between" align="start">
      <div>
        <Group gap="xs">
          <Title order={2}>{person.name}</Title>
          {person.status === 'departed' && <Badge color="red">Departed {formatDate(person.departed_at)}</Badge>}
          {person.segments.length === 0 && (
            <Badge color="gray">Not in a tracked group</Badge>
          )}
        </Group>
        <Text>
          {person.title ?? 'Title unknown'} ·{' '}
          <Anchor component={Link} to={`/people?organisation=${person.org_id}`} inherit>
            {person.org_name}
          </Anchor>
          {person.offices.map((office) => (
            <span key={office}>
              {' · '}
              <Anchor component={Link} to={`/people?organisation=${person.org_id}&office=${encodeURIComponent(office)}`} inherit c="dimmed">
                {office}
              </Anchor>
            </span>
          ))}
        </Text>
        <Group gap="md" mt={4}>
          <Anchor href={person.profile_url} target="_blank" rel="noreferrer" size="sm">
            Their page on the {words.org.toLowerCase()}'s website <IconExternalLink size={12} />
          </Anchor>
          <Group gap={4}>
            <Text size="sm" c="dimmed">
              Last checked
            </Text>
            <Freshness value={person.last_fetched} />
          </Group>
        </Group>
      </div>
      <Button variant="default" leftSection={<IconRefresh size={16} />} loading={refresh.isPending} onClick={() => refresh.mutate()}>
        Check again now
      </Button>
    </Group>
  )
}

function BriefCard({ person }: { person: PersonDetail }) {
  const queryClient = useQueryClient()
  const generate = useMutation({
    mutationFn: () => unwrap(api.POST('/api/people/{person_id}/brief', { params: { path: { person_id: person.id } } })),
    onSuccess: (brief) =>
      queryClient.setQueryData(['person', person.id], (old: PersonDetail | undefined) => (old ? { ...old, brief } : old)),
    onError: (error) => notifications.show({ color: 'orange', title: 'Brief unavailable', message: error.message }),
  })
  const brief = person.brief

  return (
    <Card withBorder padding="lg" h="100%">
      <Group justify="space-between" mb="sm">
        <Group gap="xs">
          <IconSparkles size={18} />
          <Title order={4}>Before you call</Title>
        </Group>
        {brief && <BriefMeta brief={brief} />}
      </Group>
      {!brief && (
        <Stack align="start">
          <Tooltip label="An admin has switched AI summaries off" disabled={person.ai_brief_enabled}>
            <Button leftSection={<IconSparkles size={16} />} loading={generate.isPending} disabled={!person.ai_brief_enabled} onClick={() => generate.mutate()}>
              Write summary
            </Button>
          </Tooltip>
        </Stack>
      )}
      {brief && (
        <Stack gap="sm">
          {brief.stale && (
            <Alert variant="light" color="yellow" icon={<IconInfoCircle size={16} />} p="xs">
              <Group justify="space-between">
                <Text size="sm">Their page has changed since this summary was written.</Text>
                <Button size="xs" variant="light" loading={generate.isPending} disabled={!person.ai_brief_enabled} onClick={() => generate.mutate()}>
                  Rewrite summary
                </Button>
              </Group>
            </Alert>
          )}
          <Text>{brief.brief.summary}</Text>
          <BriefSection title="Things to be aware of" items={brief.brief.be_aware_of ?? []} facts={brief.facts} warn />
          <BriefSection title="Career" items={brief.brief.career ?? []} facts={brief.facts} />
          <BriefSection title="Recent changes" items={brief.brief.recent_changes ?? []} facts={brief.facts} />
          <BriefSection title="Talking points" items={brief.brief.talking_points ?? []} facts={brief.facts} />
          {brief.brief.opening_line && (
            <Card bg="var(--mantine-color-default-hover)" padding="sm">
              <Text size="xs" c="dimmed" fw={600} tt="uppercase">
                Suggested opening line
              </Text>
              <Text fs="italic">“{brief.brief.opening_line}”</Text>
            </Card>
          )}
          <MissingSection items={brief.brief.unknowns ?? []} />
          <Feedback brief={brief} personId={person.id} />
        </Stack>
      )}
    </Card>
  )
}

/** Details the AI looked for but their page doesn't give, e.g. "education". */
function MissingSection({ items }: { items: string[] }) {
  if (!items.length) return null
  return (
    <div>
      <Text size="sm" fw={700} c="dimmed">
        Not on their page
      </Text>
      <List size="sm" spacing={4} c="dimmed">
        {items.map((item) => (
          <List.Item key={item}>{item}</List.Item>
        ))}
      </List>
    </div>
  )
}

function BriefMeta({ brief }: { brief: BriefOut }) {
  const costNote = brief.cached ? 'Reused from earlier, so this view cost nothing.' : `Writing it cost ${money(brief.cost_usd, 6)}.`
  return (
    <Tooltip multiline w={260} label={`Written by ${brief.model} on ${formatDate(brief.created_at)}. ${costNote}`}>
      <Badge variant="light" color="gray">
        AI · {formatDate(brief.created_at)}
      </Badge>
    </Tooltip>
  )
}

function BriefSection({
  title,
  items,
  facts,
  warn = false,
}: {
  title: string
  items: Schemas['BriefItem'][]
  facts: Schemas['Fact'][]
  warn?: boolean
}) {
  if (!items.length) return null
  // Citations show as small numbers (1, 2...) in the order the facts were given, not as internal ids.
  const byId = new Map(facts.map((fact, index) => [fact.id, { ...fact, number: index + 1 }]))
  return (
    <div>
      <Text size="sm" fw={700} c={warn ? 'orange' : undefined}>
        {warn && <IconAlertTriangle size={14} style={{ verticalAlign: -2, marginRight: 4 }} />}
        {title}
      </Text>
      <List size="sm" spacing={4}>
        {items.map((item) => (
          <List.Item key={item.text}>
            {item.text}{' '}
            {(item.facts ?? []).map((factId) => {
              const fact = byId.get(factId)
              if (!fact) return null // a citation to a fact we didn't supply is dropped, not shown as evidence
              return (
                <Tooltip key={factId} label={`Source: ${fact.text}${fact.source ? ' (click to open)' : ''}`} multiline w={320}>
                  <Badge
                    size="xs"
                    variant="outline"
                    component={fact.source ? 'a' : 'span'}
                    href={fact.source ?? undefined}
                    target="_blank"
                    style={{ cursor: fact.source ? 'pointer' : 'help' }}
                    aria-label={`Source ${fact.number}`}
                  >
                    {fact.number}
                  </Badge>
                </Tooltip>
              )
            })}
          </List.Item>
        ))}
      </List>
    </div>
  )
}

function Feedback({ brief, personId }: { brief: BriefOut; personId: string }) {
  const queryClient = useQueryClient()
  const send = useMutation({
    mutationFn: (rating: 1 | -1) =>
      unwrap(api.POST('/api/briefs/{output_id}/feedback', { params: { path: { output_id: brief.output_id } }, body: { rating } })).then(
        () => rating,
      ),
    onSuccess: (rating) =>
      queryClient.setQueryData(['person', personId], (old: PersonDetail | undefined) =>
        old?.brief ? { ...old, brief: { ...old.brief, feedback: rating } } : old,
      ),
    onError: (error) => notifications.show({ color: 'red', title: 'Feedback not saved', message: error.message }),
  })
  return (
    <Group gap={4}>
      <Text size="xs" c="dimmed">
        Was this summary useful?
      </Text>
      <ActionIcon variant={brief.feedback === 1 ? 'filled' : 'subtle'} size="sm" onClick={() => send.mutate(1)} aria-label="Useful">
        <IconThumbUp size={14} />
      </ActionIcon>
      <ActionIcon variant={brief.feedback === -1 ? 'filled' : 'subtle'} size="sm" onClick={() => send.mutate(-1)} aria-label="Not useful">
        <IconThumbDown size={14} />
      </ActionIcon>
    </Group>
  )
}

function Fact({ label, field, person, children }: { label: string; field: string; person: PersonDetail; children: ReactNode }) {
  const source = person.provenance[field]
  const words = useWords()
  return (
    <Group align="start" wrap="nowrap" gap="sm">
      <Text size="sm" c="dimmed" w={110} style={{ flexShrink: 0 }}>
        {label}
      </Text>
      <div style={{ flex: 1 }}>{children}</div>
      {source && (
        <Tooltip label={`Taken from their page on the ${words.org.toLowerCase()}'s website, ${formatDate(source.observed_at)}`}>
          <IconInfoCircle size={14} style={{ flexShrink: 0, opacity: 0.6 }} />
        </Tooltip>
      )}
    </Group>
  )
}

function FactsCard({ person }: { person: PersonDetail }) {
  const words = useWords()
  const fields = useMeta().data?.fields ?? []
  return (
    <Card withBorder padding="lg">
      <Title order={5} mb="sm">
        Profile details
      </Title>
      <Stack gap={8}>
        <Fact label={words.groups} field="groups" person={person}>
          <Text size="sm">{person.groups_raw.join(', ') || '-'}</Text>
        </Fact>
        <Fact label="Education" field="education" person={person}>
          {person.education.length ? person.education.map((e) => <Text size="sm" key={e.raw}>{e.raw}</Text>) : <Text size="sm">-</Text>}
        </Fact>
        {/* The domain profile's own fields (bar admissions, nationality...). */}
        {fields.map((f) => (
          <Fact key={f.key} label={f.label} field={f.key} person={person}>
            <Text size="sm">{person.extra[f.key]?.join('; ') || '-'}</Text>
          </Fact>
        ))}
        <Fact label="Contact" field="email" person={person}>
          <Text size="sm">
            {person.email ? <Anchor href={`mailto:${person.email}`}>{person.email}</Anchor> : '-'}
            {person.phone ? ` · ${person.phone}` : ''}
          </Text>
        </Fact>
      </Stack>
    </Card>
  )
}

function SignalsCard({ signals }: { signals: PersonDetail['signals'] }) {
  if (!signals.length) return null
  return (
    <Card withBorder padding="lg">
      <Group gap={6} mb="sm">
        <Title order={5}>Worth knowing</Title>
      </Group>
      <Stack gap={6}>
        {signals.map((signal) => (
          <Group key={signal.id} gap="xs" wrap="nowrap" align="start">
            {signal.caution ? (
              <IconAlertTriangle size={16} color="var(--mantine-color-orange-6)" style={{ flexShrink: 0, marginTop: 2 }} aria-label="caution" />
            ) : (
              <IconInfoCircle size={16} style={{ flexShrink: 0, marginTop: 2, opacity: 0.6 }} />
            )}
            <Text size="sm">{signal.text}</Text>
          </Group>
        ))}
      </Stack>
    </Card>
  )
}

function CareerCard({ person }: { person: PersonDetail }) {
  const words = useWords()
  return (
    <Card withBorder padding="lg" h="100%">
      <Title order={5} mb="sm">
        Career history
      </Title>
      <Timeline active={0} bulletSize={14} lineWidth={2}>
        <Timeline.Item title={`${person.title ?? 'Current role'}, ${person.org_name}`}>
          <Text size="xs" c="dimmed">
            {person.baseline ? `Already there when we started watching (${formatDate(person.first_seen)})` : `Since about ${formatDate(person.first_seen)}`}
          </Text>
        </Timeline.Item>
        {[...person.history].reverse().map((role) => (
          <Timeline.Item key={role.profile_url} title={`${role.title ?? words.person}, ${role.org_name}`}>
            <Text size="xs" c="dimmed">
              until about {formatDate(role.last_seen)}
            </Text>
          </Timeline.Item>
        ))}
      </Timeline>
    </Card>
  )
}

function ChangesCard({ events }: { events: PersonDetail['events'] }) {
  return (
    <Card withBorder padding="lg" h="100%">
      <Title order={5} mb="sm">
        Recent changes
      </Title>
      {events.length === 0 && (
        <Text size="sm" c="dimmed">
          No changes since we started watching.
        </Text>
      )}
      <Stack gap="xs">
        {events.map((event) => (
          <Group key={event.id} gap="xs" wrap="nowrap" align="start">
            <EventBadge type={event.type} />
            <div>
              <Text size="sm">{event.summary}</Text>
              <Text size="xs" c="dimmed">
                {formatDate(event.detected_at)}
              </Text>
            </div>
          </Group>
        ))}
      </Stack>
    </Card>
  )
}

/** What the team knows that their page doesn't say: calls, tips, preferences. */
function NotesCard({ person }: { person: PersonDetail }) {
  return (
    <Card withBorder padding="lg">
      <Group gap={6} mb="sm">
        <IconNotes size={18} />
        <Title order={5}>Team notes</Title>
      </Group>
      <NotesPanel personId={person.id} notes={person.notes} />
    </Card>
  )
}
