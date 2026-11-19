import { BarChart } from '@mantine/charts'
import {
  Alert,
  Anchor,
  Badge,
  Button,
  Code,
  Group,
  Loader,
  Paper,
  SegmentedControl,
  Select,
  SimpleGrid,
  Stack,
  Table,
  Tabs,
  Text,
  Title,
  Tooltip,
  UnstyledButton,
  useComputedColorScheme,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Fragment, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { api, unwrap, type Schemas } from '../api/client'
import { EmptyState, PageTitle } from '../components/common'
import { useMeta, useUrlFilters } from '../lib'

type Job = Schemas['JobOut']
type Worker = Schemas['WorkerOut']

// The page polls; react-query pauses polling while the browser tab is hidden.
const REFRESH_MS = 5000

const WORKER_COLOR: Record<Worker['status'], string> = { running: 'teal', idle: 'orange', stopped: 'gray', stale: 'red' }
const JOB_COLOR: Record<Job['status'], string> = { queued: 'blue', running: 'indigo', done: 'teal', failed: 'red', cancelled: 'gray' }
const EVENT_COLOR: Record<string, string> = { WARNING: 'orange', ERROR: 'red', CRITICAL: 'red' }

// Done vs failed per hour. Checked with the dataviz palette validator (light and dark, incl.
// colour-blind separation); green-vs-red failed it, so "done" is the app's blue.
const DONE_COLOR = { light: '#2a78d6', dark: '#3987e5' }
const FAILED_COLOR = '#d03b3b'

const LOG_STATUSES = [
  { value: 'finished', label: 'Finished' },
  { value: 'failed', label: 'Failed' },
  { value: 'queued', label: 'Queued' },
  { value: 'running', label: 'Running' },
  { value: 'cancelled', label: 'Cancelled' },
]
const EVENT_LEVELS = [
  { value: 'info', label: 'Everything' },
  { value: 'warning', label: 'Warnings' },
  { value: 'error', label: 'Errors' },
]
// The job log's filters; the page and the log share them through the URL.
const LOG_FILTERS = { tab: 'overview', status: 'finished', kind: '', org: '' }
const PAGE_SIZE = 50
const MAX_ROWS = 500 // what the API will return in one go

const KINDS = [
  { value: 'discover', label: 'Discover' },
  { value: 'fetch', label: 'Fetch' },
  { value: 'reprocess', label: 'Reprocess' },
]

/** "45s", "12m", "3h 5m", "2d": short durations for a busy table. */
function span(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ${m % 60}m`
  return `${Math.floor(h / 24)}d`
}

function between(from: string | null | undefined, to: string | null | undefined): string {
  if (!from || !to) return '-'
  return span(new Date(to).getTime() - new Date(from).getTime())
}

function ago(value: string | null | undefined, now: string): string {
  if (!value) return '-'
  const ms = new Date(now).getTime() - new Date(value).getTime()
  return ms >= 0 ? `${span(ms)} ago` : `in ${span(-ms)}`
}

function clock(value: string | null | undefined): string {
  if (!value) return ''
  return new Date(value).toLocaleString('en-GB', { dateStyle: 'medium', timeStyle: 'medium' })
}

/** A relative time with the exact time on hover. */
function When({ value, now, size = 'sm' }: { value: string | null | undefined; now: string; size?: string }) {
  if (!value) return <Text size={size} c="dimmed" span>-</Text>
  return (
    <Tooltip label={clock(value)}>
      <Text size={size} span>
        {ago(value, now)}
      </Text>
    </Tooltip>
  )
}

export function WorkerPage() {
  // The open tab is kept in the URL, so Back and a refresh return to it.
  const { values, update } = useUrlFilters(LOG_FILTERS)
  const status = useQuery({
    queryKey: ['admin', 'worker'],
    queryFn: () => unwrap(api.GET('/api/admin/worker')),
    refetchInterval: REFRESH_MS,
  })

  if (status.isPending) return <Loader />
  if (status.error) return <Alert color="red">{status.error.message}</Alert>
  const failed = status.data.queue.reduce((sum, row) => sum + row.failed_24h, 0)
  // The tiles open the log already filtered, e.g. "Failed, 24h" → failed jobs.
  const openLog = (logStatus: string) => update({ tab: 'log', status: logStatus, kind: null, org: null })

  return (
    <Stack gap="lg">
      <Group justify="space-between" align="baseline">
        <PageTitle title="Worker" />
        <Text size="xs" c="dimmed">
          Refreshes every {REFRESH_MS / 1000}s
        </Text>
      </Group>
      <Tabs value={values.tab} onChange={(tab) => update({ tab: tab ?? 'overview' })} keepMounted={false}>
        <Tabs.List mb="md">
          <Tabs.Tab value="overview">Overview</Tabs.Tab>
          <Tabs.Tab
            value="log"
            rightSection={
              failed > 0 ? (
                <Badge size="xs" color="red" variant="light" aria-label={`${failed} failed in 24 hours`}>
                  {failed}
                </Badge>
              ) : undefined
            }
          >
            Job log
          </Tabs.Tab>
          <Tabs.Tab value="events">Events</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="overview">
          <Overview data={status.data} onOpenLog={openLog} />
        </Tabs.Panel>
        <Tabs.Panel value="log">
          <JobLog />
        </Tabs.Panel>
        <Tabs.Panel value="events">
          <WorkerEvents />
        </Tabs.Panel>
      </Tabs>
    </Stack>
  )
}

function Overview({ data, onOpenLog }: { data: Schemas['WorkerStatusOut']; onOpenLog: (status: string) => void }) {
  const { now, workers, queue, by_org, blocked, oldest_due_at, throughput } = data
  const total = (key: 'due' | 'waiting' | 'running' | 'done_24h' | 'failed_24h') => queue.reduce((sum, row) => sum + row[key], 0)
  const live = workers.filter((w) => w.status === 'running' || w.status === 'idle')
  const earlier = workers.filter((w) => w.status === 'stopped' || w.status === 'stale')
  const orgsReady = by_org.filter((row) => row.due > 0 || row.running > 0).length

  return (
    <Stack gap="lg">
      {live.length === 0 && (
        <Alert color="red" title="No worker is running">
          No worker has reported in for the last 30 seconds, so nothing in the queue will run. Check the worker container
          (<Code>docker compose ps worker</Code>, <Code>docker compose logs worker</Code>).
        </Alert>
      )}
      {live
        .filter((w) => w.status === 'idle')
        .map((w) => (
          <Alert key={w.id} color="orange" title="The worker is idle">
            {w.reason}
          </Alert>
        ))}

      <SimpleGrid cols={{ base: 2, sm: 3, lg: 6 }}>
        <Stat label="Ready to run" value={total('due')} hint={oldest_due_at ? `oldest waited ${span(new Date(now).getTime() - new Date(oldest_due_at).getTime())}` : 'nothing waiting'} onClick={() => onOpenLog('queued')} />
        <Stat label="Scheduled later" value={total('waiting')} hint="retry backoff or delay" onClick={() => onOpenLog('queued')} />
        <Stat label="Running" value={total('running')} onClick={() => onOpenLog('running')} />
        <Stat label="Done, 24h" value={total('done_24h')} onClick={() => onOpenLog('finished')} />
        <Stat label="Failed, 24h" value={total('failed_24h')} color={total('failed_24h') ? 'red' : undefined} onClick={() => onOpenLog('failed')} />
        <Stat label="Blocked orgs" value={blocked.length} color={blocked.length ? 'orange' : undefined} hint="paused 24 h after a 403/429" />
      </SimpleGrid>

      {live.map((w) => (
        <WorkerCard key={w.id} worker={w} now={now} totalDue={total('due')} orgsReady={orgsReady} />
      ))}
      {earlier.length > 0 && <EarlierWorkers workers={earlier} now={now} />}

      {blocked.length > 0 && (
        <Section title="Blocked organisations" summary="paused for 24 hours after their site answered 403 or 429">
          <Table.ScrollContainer minWidth={500}>
            <Table>
              <Table.Tbody>
                {blocked.map((b) => (
                  <Table.Tr key={b.org_id}>
                    <Table.Td>{b.name ?? b.org_id}</Table.Td>
                    <Table.Td>
                      <Text size="sm" c="dimmed" lineClamp={2}>
                        {b.reason}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      resumes <When value={b.blocked_until} now={now} />
                    </Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </Section>
      )}

      <SimpleGrid cols={{ base: 1, lg: 2 }}>
        <Section title="Waiting work by organisation" summary="one lane per organisation at a time">
          {by_org.length === 0 ? (
            <Text size="sm" c="dimmed">
              Nothing is queued.
            </Text>
          ) : (
            <Table.ScrollContainer minWidth={320}>
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Organisation</Table.Th>
                    <Table.Th ta="right">Ready</Table.Th>
                    <Table.Th ta="right">Later</Table.Th>
                    <Table.Th ta="right">Running</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {by_org.map((row) => (
                    <Table.Tr key={row.org_id}>
                      <Table.Td>{row.name ?? row.org_id}</Table.Td>
                      <Table.Td ta="right">{row.due.toLocaleString()}</Table.Td>
                      <Table.Td ta="right">{row.waiting.toLocaleString()}</Table.Td>
                      <Table.Td ta="right">{row.running}</Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          )}
        </Section>

        <Section title="Queue by kind">
          {queue.length === 0 ? (
            <Text size="sm" c="dimmed">
              The queue is empty.
            </Text>
          ) : (
            <Table.ScrollContainer minWidth={420}>
              <Table>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>Kind</Table.Th>
                    <Table.Th ta="right">Ready</Table.Th>
                    <Table.Th ta="right">Later</Table.Th>
                    <Table.Th ta="right">Running</Table.Th>
                    <Table.Th ta="right">Done 24h</Table.Th>
                    <Table.Th ta="right">Failed 24h</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {queue.map((row) => (
                    <Table.Tr key={row.kind}>
                      <Table.Td>{row.kind}</Table.Td>
                      <Table.Td ta="right">{row.due.toLocaleString()}</Table.Td>
                      <Table.Td ta="right">{row.waiting.toLocaleString()}</Table.Td>
                      <Table.Td ta="right">{row.running}</Table.Td>
                      <Table.Td ta="right">{row.done_24h.toLocaleString()}</Table.Td>
                      <Table.Td ta="right" c={row.failed_24h ? 'red' : undefined}>
                        {row.failed_24h}
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          )}
        </Section>
      </SimpleGrid>

      <Section title="Jobs finished per hour" summary="last 24 hours">
        <ThroughputChart hours={throughput} />
      </Section>
    </Stack>
  )
}

/** A live worker: its busy lanes in full, its idle lanes in one line with the reason they're idle. */
function WorkerCard({ worker: w, now, totalDue, orgsReady }: { worker: Worker; now: string; totalDue: number; orgsReady: number }) {
  const busy = w.lanes.filter((lane) => lane.job_id)
  const idle = w.lanes.length - busy.length
  let why = ''
  if (idle > 0 && totalDue === 0) why = 'Nothing is ready to run.'
  else if (idle > 0)
    why =
      `Only ${orgsReady} organisation${orgsReady === 1 ? ' has' : 's have'} work ready, and each organisation is crawled by one lane at a time ` +
      '(so its website gets at most one request every few seconds). More lanes only help when more organisations have work.'
  return (
    <Section
      title="Worker"
      summary={`${busy.length} of ${w.lanes.length} lanes busy`}
      right={
        <Group gap="xs">
          <Badge color={WORKER_COLOR[w.status]}>{w.status}</Badge>
          <Tooltip label={`${w.id} · ${w.user_agent ?? ''}`} multiline maw={400}>
            <Text size="xs" c="dimmed">
              up {between(w.started_at, w.last_seen)} · seen <When value={w.last_seen} now={now} size="xs" />
            </Text>
          </Tooltip>
        </Group>
      }
    >
      {busy.length > 0 && (
        <Table.ScrollContainer minWidth={420}>
          <Table>
            <Table.Tbody>
              {busy.map((lane) => (
                <Table.Tr key={lane.lane}>
                  <Table.Td w={70}>
                    <Text size="sm" c="dimmed">
                      lane {lane.lane}
                    </Text>
                  </Table.Td>
                  <Table.Td w={90}>
                    <Badge variant="light" size="sm">
                      {lane.kind}
                    </Badge>
                  </Table.Td>
                  <Table.Td>
                    <LaneTarget lane={lane} />
                  </Table.Td>
                  <Table.Td ta="right">
                    <Text size="sm" c="dimmed">
                      {lane.started_at ? span(new Date(now).getTime() - new Date(lane.started_at).getTime()) : '-'}
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
      {idle > 0 && (
        <Text size="sm" c="dimmed" mt={busy.length ? 'xs' : 0}>
          <Text span fw={600} c="dimmed">
            {idle} lane{idle === 1 ? '' : 's'} idle.
          </Text>{' '}
          {why}
        </Text>
      )}
      {w.paused_orgs.length > 0 && (
        <Text size="xs" c="orange" mt="xs">
          Paused: {w.paused_orgs.join(', ')}
        </Text>
      )}
    </Section>
  )
}

function LaneTarget({ lane }: { lane: Schemas['LaneOut'] }) {
  const org = lane.org_name ?? lane.org_id
  if (!lane.person_id) return <Text size="sm">{org}</Text>
  return (
    <Text size="sm">
      <Anchor component={Link} to={`/people/${lane.person_id}`} size="sm">
        {lane.person_name ?? lane.person_id}
      </Anchor>
      <Text span c="dimmed" size="xs">
        {' '}
        · {org}
      </Text>
    </Text>
  )
}

/** Worker processes that have stopped (each rebuild leaves one), folded away by default. */
function EarlierWorkers({ workers, now }: { workers: Worker[]; now: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div>
      <Anchor size="sm" component="button" c="dimmed" onClick={() => setOpen(!open)}>
        {open ? 'Hide' : 'Show'} {workers.length} earlier worker process{workers.length === 1 ? '' : 'es'} (stopped or stale)
      </Anchor>
      {open && (
        <Table.ScrollContainer minWidth={420} mt="xs">
          <Table>
            <Table.Tbody>
              {workers.map((w) => (
                <Table.Tr key={w.id}>
                  <Table.Td w={90}>
                    <Tooltip label={w.status === 'stale' ? 'Stopped sending heartbeats without saying it was stopping (killed, crashed, or the container was removed)' : 'Shut down cleanly'} multiline maw={300}>
                      <Badge size="sm" color={WORKER_COLOR[w.status]}>
                        {w.status}
                      </Badge>
                    </Tooltip>
                  </Table.Td>
                  <Table.Td>
                    <Text size="xs" ff="monospace">
                      {w.id}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="xs" c="dimmed">
                      ran {between(w.started_at, w.last_seen)}, last seen <When value={w.last_seen} now={now} size="xs" />
                    </Text>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
    </div>
  )
}

/** The worker log: finished jobs newest first, or the queue / running jobs, with filters kept in the URL. */
function JobLog() {
  const meta = useMeta()
  const { values, update } = useUrlFilters(LOG_FILTERS)
  const isFiltered = values.status !== 'finished' || values.kind !== '' || values.org !== ''
  const [rows, setRows] = useState(PAGE_SIZE)
  // A new filter starts again from the first page.
  const filter = (patch: Partial<Record<'status' | 'kind' | 'org', string | null>>) => {
    setRows(PAGE_SIZE)
    update(patch)
  }
  const log = useQuery({
    queryKey: ['admin', 'jobs', values.status, values.kind, values.org, rows],
    placeholderData: keepPreviousData, // keep showing the current rows while more load
    queryFn: () =>
      unwrap(
        api.GET('/api/admin/jobs', {
          params: {
            query: {
              status: values.status as 'finished' | 'queued' | 'running' | 'failed' | 'cancelled',
              kind: values.kind || undefined,
              org: values.org || undefined,
              limit: rows,
            },
          },
        }),
      ),
    refetchInterval: REFRESH_MS,
  })
  // Ages are measured from when this list was fetched (it refreshes every few seconds anyway).
  const now = new Date(log.dataUpdatedAt).toISOString()
  const mode = values.status === 'queued' ? 'queued' : values.status === 'running' ? 'running' : 'finished'

  return (
    <Section title="Job log" summary="Finished jobs are kept for 7 days">
      <Group mb="sm" gap="sm" wrap="wrap">
        <Select size="xs" aria-label="Status" data={LOG_STATUSES} value={values.status} onChange={(status) => filter({ status: status ?? 'finished' })} allowDeselect={false} w={130} />
        <Select size="xs" placeholder="Any kind" data={KINDS} value={values.kind || null} onChange={(kind) => filter({ kind: kind ?? '' })} clearable w={140} />
        <Select
          size="xs"
          placeholder="Any organisation"
          data={(meta.data?.organisations ?? []).map((f) => ({ value: f.id, label: f.name }))}
          value={values.org || null}
          onChange={(org) => filter({ org: org ?? '' })}
          clearable
          searchable
          w={220}
        />
        {isFiltered && (
          <Anchor size="xs" component="button" onClick={() => filter({ status: null, kind: null, org: null })}>
            Clear filters
          </Anchor>
        )}
      </Group>
      {log.isPending ? (
        <Loader size="sm" />
      ) : log.error ? (
        <Alert color="red">{log.error.message}</Alert>
      ) : log.data.length === 0 ? (
        <EmptyState title="No jobs match" />
      ) : (
        <>
          <Table.ScrollContainer minWidth={800}>
            <JobTable jobs={log.data} now={now} mode={mode} />
          </Table.ScrollContainer>
          <ShowMore shown={log.data.length} rows={rows} loading={log.isFetching} onMore={() => setRows(rows + PAGE_SIZE)} />
        </>
      )}
    </Section>
  )
}

function JobTable({ jobs, now, mode }: { jobs: Job[]; now: string; mode: 'finished' | 'queued' | 'running' }) {
  return (
    <Table striped highlightOnHover>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>{mode === 'finished' ? 'Finished' : mode === 'queued' ? 'Runs' : 'Started'}</Table.Th>
          <Table.Th>Status</Table.Th>
          <Table.Th>Kind</Table.Th>
          <Table.Th>What</Table.Th>
          <Table.Th ta="right">Tries</Table.Th>
          <Table.Th ta="right">{mode === 'queued' ? 'Priority' : 'Took'}</Table.Th>
          <Table.Th>Result</Table.Th>
          {mode !== 'running' && <Table.Th />}
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {jobs.map((job) => (
          <Table.Tr key={job.id}>
            <Table.Td>
              <When value={mode === 'finished' ? job.finished_at : mode === 'queued' ? job.run_after : job.started_at} now={now} />
            </Table.Td>
            <Table.Td>
              <Badge size="sm" color={JOB_COLOR[job.status]} variant="light">
                {job.status}
              </Badge>
            </Table.Td>
            <Table.Td>
              <Text size="sm">
                {job.kind}
                {job.reason && (
                  <Text span c="dimmed" size="xs">
                    {' '}
                    ({job.reason})
                  </Text>
                )}
              </Text>
            </Table.Td>
            <Table.Td>
              <Target job={job} />
            </Table.Td>
            <Table.Td ta="right">{job.attempts}</Table.Td>
            <Table.Td ta="right">
              {mode === 'queued' ? job.priority : mode === 'running' ? between(job.started_at, now) : between(job.started_at, job.finished_at)}
            </Table.Td>
            <Table.Td maw={380}>
              {job.error ? (
                <Tooltip label={job.error} multiline maw={500}>
                  <Text size="xs" c="red" lineClamp={2}>
                    {job.error}
                  </Text>
                </Tooltip>
              ) : (
                <Text size="xs" c="dimmed" ff="monospace" lineClamp={2}>
                  {job.note ?? ''}
                </Text>
              )}
            </Table.Td>
            {mode !== 'running' && (
              <Table.Td>
                <JobActions job={job} />
              </Table.Td>
            )}
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  )
}

/** Who or what a job is about: the person (linked) for a fetch, otherwise the organisation. */
function Target({ job }: { job: Job }) {
  const org = job.org_name ?? job.org_id
  if (!job.person_id) return <Text size="sm">{org}</Text>
  return (
    <Text size="sm">
      <Anchor component={Link} to={`/people/${job.person_id}`} size="sm">
        {job.person_name ?? job.person_id}
      </Anchor>
      <Text span c="dimmed" size="xs">
        {' '}
        · {org}
      </Text>
    </Text>
  )
}

type Action = 'retry' | 'cancel' | 'run-now'
const ACTION_DONE: Record<Action, string> = { retry: 'Queued to run next', cancel: 'Cancelled', 'run-now': 'Moved to the front of the queue' }

/** Retry a failed or cancelled job; run a queued one now, or cancel it. Running jobs can't be stopped. */
function JobActions({ job }: { job: Job }) {
  const queryClient = useQueryClient()
  const act = useMutation({
    mutationFn: (action: Action) => {
      const params = { params: { path: { job_id: job.id } } }
      if (action === 'retry') return unwrap(api.POST('/api/admin/jobs/{job_id}/retry', params))
      if (action === 'cancel') return unwrap(api.POST('/api/admin/jobs/{job_id}/cancel', params))
      return unwrap(api.POST('/api/admin/jobs/{job_id}/run-now', params))
    },
    onSuccess: (_job, action) => {
      notifications.show({ message: ACTION_DONE[action], color: 'teal' })
      void queryClient.invalidateQueries({ queryKey: ['admin', 'worker'] })
      void queryClient.invalidateQueries({ queryKey: ['admin', 'jobs'] })
    },
    onError: (error) => notifications.show({ title: 'That didn\'t work', message: error.message, color: 'red' }),
  })
  const button = (action: Action, label: string, hint: string) => (
    <Tooltip label={hint} multiline maw={260}>
      <Button size="compact-xs" variant="light" color={action === 'cancel' ? 'gray' : undefined} loading={act.isPending && act.variables === action} disabled={act.isPending} onClick={() => act.mutate(action)}>
        {label}
      </Button>
    </Tooltip>
  )
  if (job.status === 'failed' || job.status === 'cancelled') {
    return button('retry', 'Retry', 'Queue it again with fresh attempts, ahead of routine work')
  }
  if (job.status === 'queued') {
    return (
      <Group gap={4} wrap="nowrap">
        {button('run-now', 'Run now', 'Skip any wait and put it at the front of the queue')}
        {button('cancel', 'Cancel', 'Take it off the queue (Retry puts it back). The scheduler may queue a discovery again by itself.')}
      </Group>
    )
  }
  return null
}

function ThroughputChart({ hours }: { hours: Schemas['HourRow'][] }) {
  const scheme = useComputedColorScheme('light')
  const [asTable, setAsTable] = useState(false)
  const rows = hours.map((h) => ({
    hour: new Date(h.hour).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' }),
    done: h.done,
    failed: h.failed,
  }))
  const total = hours.reduce((sum, h) => sum + h.done + h.failed, 0)
  return (
    <div>
      <Group justify="flex-end" mb="xs">
        <Button size="xs" variant="subtle" onClick={() => setAsTable(!asTable)}>
          {asTable ? 'Show chart' : 'Show as table'}
        </Button>
      </Group>
      {total === 0 ? (
        <Text size="sm" c="dimmed">
          No jobs finished in the last 24 hours.
        </Text>
      ) : asTable ? (
        <Table.ScrollContainer minWidth={300} mah={320}>
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Hour</Table.Th>
                <Table.Th ta="right">Done</Table.Th>
                <Table.Th ta="right">Failed</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {rows.map((r) => (
                <Table.Tr key={r.hour}>
                  <Table.Td>{r.hour}</Table.Td>
                  <Table.Td ta="right">{r.done}</Table.Td>
                  <Table.Td ta="right">{r.failed}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      ) : (
        <BarChart
          h={220}
          data={rows}
          dataKey="hour"
          type="stacked"
          withLegend
          legendProps={{ verticalAlign: 'top', height: 32 }}
          series={[
            { name: 'done', label: 'Done', color: DONE_COLOR[scheme] },
            { name: 'failed', label: 'Failed', color: FAILED_COLOR },
          ]}
          gridAxis="y"
          tickLine="none"
          maxBarWidth={20}
          // A thin surface-coloured outline keeps the stacked segments visibly apart.
          barProps={{ stroke: 'var(--mantine-color-body)', strokeWidth: 2 }}
        />
      )}
    </div>
  )
}

/** The worker's own log lines (start/stop, discoveries, warnings, crashes with tracebacks). */
function WorkerEvents() {
  const [level, setLevel] = useState<'info' | 'warning' | 'error'>('info')
  const [open, setOpen] = useState<string | null>(null)
  const [rows, setRows] = useState(PAGE_SIZE)
  const events = useQuery({
    queryKey: ['admin', 'worker-events', level, rows],
    placeholderData: keepPreviousData,
    queryFn: () => unwrap(api.GET('/api/admin/worker/events', { params: { query: { level, limit: rows } } })),
    refetchInterval: REFRESH_MS,
  })
  const now = new Date(events.dataUpdatedAt).toISOString()
  return (
    <Section title="Worker events" summary="The worker's log, kept for 7 days">
      <SegmentedControl size="xs" mb="sm" data={EVENT_LEVELS} value={level} onChange={(value) => {
          setLevel(value as typeof level)
          setRows(PAGE_SIZE)
        }} />
      {events.isPending ? (
        <Loader size="sm" />
      ) : events.error ? (
        <Alert color="red">{events.error.message}</Alert>
      ) : events.data.length === 0 ? (
        <EmptyState title="Nothing logged" />
      ) : (
        <>
        <Table.ScrollContainer minWidth={700}>
          <Table>
            <Table.Tbody>
              {events.data.map((e) => (
                <Fragment key={e.id}>
                  <Table.Tr>
                    <Table.Td w={110}>
                      <When value={e.ts} now={now} />
                    </Table.Td>
                    <Table.Td w={90}>
                      <Badge size="sm" variant="light" color={EVENT_COLOR[e.level] ?? 'gray'}>
                        {e.level.toLowerCase()}
                      </Badge>
                    </Table.Td>
                    <Table.Td w={170}>
                      <Text size="xs" c="dimmed" ff="monospace">
                        {e.logger}
                      </Text>
                    </Table.Td>
                    <Table.Td>
                      <Text size="sm" lineClamp={3} style={{ wordBreak: 'break-word' }}>
                        {e.message}
                      </Text>
                      {e.traceback && (
                        <Anchor size="xs" component="button" onClick={() => setOpen(open === e.id ? null : e.id)}>
                          {open === e.id ? 'Hide traceback' : 'Show traceback'}
                        </Anchor>
                      )}
                    </Table.Td>
                  </Table.Tr>
                  {open === e.id && e.traceback && (
                    <Table.Tr>
                      <Table.Td colSpan={4}>
                        <Code block fz="xs">
                          {e.traceback}
                        </Code>
                      </Table.Td>
                    </Table.Tr>
                  )}
                </Fragment>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
          <ShowMore shown={events.data.length} rows={rows} loading={events.isFetching} onMore={() => setRows(rows + PAGE_SIZE)} />
        </>
      )}
    </Section>
  )
}

/** "Show more" under a list that has filled its page: asks the API for the next PAGE_SIZE rows. */
function ShowMore({ shown, rows, loading, onMore }: { shown: number; rows: number; loading: boolean; onMore: () => void }) {
  if (shown < rows || rows >= MAX_ROWS) return null
  return (
    <Group justify="center" mt="sm">
      <Button size="xs" variant="subtle" loading={loading} onClick={onMore}>
        Show {PAGE_SIZE} more
      </Button>
    </Group>
  )
}

function Section({ title, summary, right, children }: { title: string; summary?: string; right?: ReactNode; children: ReactNode }) {
  return (
    <Paper withBorder p="md">
      <Group justify="space-between" mb="sm" gap="xs">
        <Group gap="sm" align="baseline">
          <Title order={5}>{title}</Title>
          {summary && (
            <Text size="xs" c="dimmed">
              {summary}
            </Text>
          )}
        </Group>
        {right}
      </Group>
      {children}
    </Paper>
  )
}

/** A headline number. With onClick the whole tile is a button (it opens the matching job list). */
function Stat({ label, value, hint, color, onClick }: { label: string; value: number; hint?: string; color?: string; onClick?: () => void }) {
  const tile = (
    <Paper withBorder p="md" h="100%">
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text fz={26} fw={700} c={color}>
        {value.toLocaleString()}
      </Text>
      {hint && (
        <Text size="xs" c="dimmed">
          {hint}
        </Text>
      )}
    </Paper>
  )
  if (!onClick) return tile
  return (
    <UnstyledButton onClick={onClick} aria-label={`${label}: ${value}. Show these jobs`}>
      {tile}
    </UnstyledButton>
  )
}
