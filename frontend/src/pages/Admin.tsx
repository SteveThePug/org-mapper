import { BarChart } from '@mantine/charts'
import {
  Accordion,
  Alert,
  Badge,
  Button,
  Group,
  Loader,
  NumberInput,
  Paper,
  Progress,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  Text,
  TextInput,
  Title,
  useComputedColorScheme,
} from '@mantine/core'
import { useLocalStorage } from '@mantine/hooks'
import { notifications } from '@mantine/notifications'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type ReactNode } from 'react'
import { api, unwrap, type Schemas } from '../api/client'
import { PageTitle } from '../components/common'
import { formatDate, money } from '../lib'

type RuntimeSettings = Schemas['RuntimeSettingsIO']

// Single-series magnitude chart: one hue, stepped separately for light and dark surfaces.
const SERIES_1 = { light: '#2a78d6', dark: '#3987e5' }

const RUN_HEALTH_COLOR: Record<string, string> = { ok: 'teal', skipped: 'gray' }

function callColor(call: Schemas['LlmCallOut']): string {
  if (call.cache_hit) return 'teal'
  return call.status === 'ok' ? 'indigo' : 'orange'
}

// The sections a first-time visitor sees open; after that, whatever the admin last left open is remembered.
const DEFAULT_OPEN = ['settings', 'spend']

export function AdminPage() {
  const usageQuery = useQuery({ queryKey: ['admin', 'usage'], queryFn: () => unwrap(api.GET('/api/admin/usage')) })
  const costsQuery = useQuery({ queryKey: ['admin', 'costs'], queryFn: () => unwrap(api.GET('/api/admin/costs')) })
  const features = useQuery({ queryKey: ['admin', 'features'], queryFn: () => unwrap(api.GET('/api/admin/feature-usage')) })
  const runs = useQuery({ queryKey: ['admin', 'runs'], queryFn: () => unwrap(api.GET('/api/admin/runs', { params: { query: { limit: 30 } } })) })
  const [open, setOpen] = useLocalStorage<string[]>({ key: 'admin-open-sections', defaultValue: DEFAULT_OPEN })

  if (usageQuery.isPending || costsQuery.isPending) return <Loader />
  if (usageQuery.error || costsQuery.error) return <Alert color="red">{(usageQuery.error ?? costsQuery.error)?.message}</Alert>
  const usage = usageQuery.data
  const costs = costsQuery.data
  const budgetUsedPct = usage.budget_today_usd ? Math.min(100, (usage.spent_today_usd / usage.budget_today_usd) * 100) : 0
  // AI-only: hosting costs are deliberately left out of this page.
  const aiCostPerProfile = costs.in_scope_profiles ? costs.llm_30d_usd / costs.in_scope_profiles : null

  return (
    <Stack gap="lg">
      <PageTitle title="AI & costs" />

      {/* The headline numbers always stay visible; everything else folds away. */}
      <SimpleGrid cols={{ base: 1, sm: 3 }}>
        <Stat label="AI spend today" value={money(usage.spent_today_usd)} hint={`daily cap ${money(usage.budget_today_usd)}`}>
          <Progress value={budgetUsedPct} color={budgetUsedPct > 80 ? 'orange' : 'indigo'} size="sm" mt={6} aria-label="Share of daily AI budget used" />
        </Stat>
        <Stat label="AI spend, 30 days" value={money(usage.spent_30d_usd)} hint={`projected month: ${money(usage.projected_month_usd)}`} />
        <Stat label="Cache hit rate" value={`${Math.round(usage.cache_hit_rate * 100)}%`} hint={`${money(usage.saved_30d_usd)} saved in 30 days`} />
      </SimpleGrid>

      <Group justify="flex-end" gap="xs" mb={-8}>
        <Button size="xs" variant="subtle" onClick={() => setOpen(SECTION_IDS)}>
          Expand all
        </Button>
        <Button size="xs" variant="subtle" onClick={() => setOpen([])}>
          Collapse all
        </Button>
      </Group>

      <Accordion multiple value={open} onChange={setOpen} variant="separated" radius="md">
        <Section id="settings" title="AI features & budget">
          <SettingsPanel />
        </Section>

        <Section id="spend" title="Spend & unit economics" summary={`${money(usage.spent_30d_usd)} over 30 days`}>
          <SimpleGrid cols={{ base: 1, lg: 2 }}>
            <SpendChart days={usage.by_day} />
            <div>
              <Title order={6} mb="sm">
                Unit economics (30 days)
              </Title>
              <Table>
                <Table.Tbody>
                  <Row label="Tracked people (in a segment)" value={costs.in_scope_profiles.toLocaleString()} />
                  <Row label="AI cost per tracked person / month" value={money(aiCostPerProfile)} />
                  <Row label="Briefs served (generated + cached)" value={`${costs.briefs_served_30d} (${costs.briefs_generated_30d} generated)`} />
                  <Row label="AI cost per brief served" value={money(costs.cost_per_brief_served_usd, 6)} />
                  <Row label="Requests made to organisation websites" value={costs.scrape_requests_30d.toLocaleString()} />
                  <Row label="Brief feedback" value={`👍 ${usage.feedback.up ?? 0} · 👎 ${usage.feedback.down ?? 0}`} />
                </Table.Tbody>
              </Table>
            </div>
          </SimpleGrid>
        </Section>

        <Section id="breakdown" title="Spend by feature & model" summary={`${usage.by_task.length} features · ${usage.by_model.length} models`}>
          <SimpleGrid cols={{ base: 1, lg: 2 }}>
            <SpendTable title="By feature" rows={usage.by_task} />
            <SpendTable title="By model" rows={usage.by_model} />
          </SimpleGrid>
        </Section>

        <Section id="features" title="Feature usage (30 days)">
          <Table>
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Action</Table.Th>
                <Table.Th ta="right">Count</Table.Th>
                <Table.Th ta="right">Users</Table.Th>
                <Table.Th>Last used</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {features.data?.map((row) => (
                <Table.Tr key={row.kind}>
                  <Table.Td>{row.kind.replaceAll('_', ' ')}</Table.Td>
                  <Table.Td ta="right">{row.count_30d}</Table.Td>
                  <Table.Td ta="right">{row.users_30d}</Table.Td>
                  <Table.Td>{formatDate(row.last_used)}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Section>

        <Section id="ledger" title="AI call ledger" summary={`latest ${usage.recent.length} calls`}>
          <Table.ScrollContainer minWidth={800}>
            <Table striped>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>When</Table.Th>
                  <Table.Th>Feature</Table.Th>
                  <Table.Th>Model</Table.Th>
                  <Table.Th>Outcome</Table.Th>
                  <Table.Th ta="right">Tokens in/out</Table.Th>
                  <Table.Th ta="right">Cost</Table.Th>
                  <Table.Th ta="right">Latency</Table.Th>
                  <Table.Th>User</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {usage.recent.map((call, i) => (
                  <Table.Tr key={`${call.ts}-${i}`}>
                    <Table.Td>{new Date(call.ts).toLocaleString('en-GB')}</Table.Td>
                    <Table.Td>{call.task}</Table.Td>
                    <Table.Td>
                      <Text size="sm">{call.model}</Text>
                    </Table.Td>
                    <Table.Td>
                      <Badge variant="light" color={callColor(call)} title={call.error ?? undefined}>
                        {call.cache_hit ? 'cache hit' : call.status.replace('_', ' ')}
                      </Badge>
                    </Table.Td>
                    <Table.Td ta="right">
                      {call.tokens_in} / {call.tokens_out}
                    </Table.Td>
                    <Table.Td ta="right">{money(call.cost_usd, 6)}</Table.Td>
                    <Table.Td ta="right">{call.latency_ms ? `${(call.latency_ms / 1000).toFixed(1)}s` : '-'}</Table.Td>
                    <Table.Td>{call.user}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </Section>

        <Section id="runs" title="Recent discovery runs" summary={runs.data ? `latest ${runs.data.length} runs` : undefined}>
          <Table.ScrollContainer minWidth={700}>
            <Table striped>
              <Table.Thead>
                <Table.Tr>
                  <Table.Th>Started</Table.Th>
                  <Table.Th>Organisation</Table.Th>
                  <Table.Th>Health</Table.Th>
                  <Table.Th ta="right">Profiles</Table.Th>
                  <Table.Th ta="right">New</Table.Th>
                  <Table.Th ta="right">Missing</Table.Th>
                  <Table.Th ta="right">Requests</Table.Th>
                </Table.Tr>
              </Table.Thead>
              <Table.Tbody>
                {runs.data?.map((run) => (
                  <Table.Tr key={run.id}>
                    <Table.Td>{new Date(run.started_at).toLocaleString('en-GB')}</Table.Td>
                    <Table.Td>{run.org_id}</Table.Td>
                    <Table.Td>
                      <Badge variant="light" color={RUN_HEALTH_COLOR[run.health] ?? 'orange'} title={run.error ?? undefined}>
                        {run.health}
                      </Badge>
                    </Table.Td>
                    <Table.Td ta="right">{run.discovered ?? '-'}</Table.Td>
                    <Table.Td ta="right">{run.new ?? '-'}</Table.Td>
                    <Table.Td ta="right">{run.missing ?? '-'}</Table.Td>
                    <Table.Td ta="right">{run.requests ?? '-'}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </Table.ScrollContainer>
        </Section>
      </Accordion>
    </Stack>
  )
}

const SECTION_IDS = ['settings', 'spend', 'breakdown', 'features', 'ledger', 'runs']

// One collapsible block: the title and a one-line summary stay visible when it's folded.
function Section({ id, title, summary, children }: { id: string; title: string; summary?: string; children: ReactNode }) {
  return (
    <Accordion.Item value={id}>
      <Accordion.Control>
        <Group gap="sm" wrap="nowrap">
          <Text fw={600}>{title}</Text>
          {summary && (
            <Text size="sm" c="dimmed" truncate>
              {summary}
            </Text>
          )}
        </Group>
      </Accordion.Control>
      <Accordion.Panel>{children}</Accordion.Panel>
    </Accordion.Item>
  )
}

function Stat({ label, value, hint, children }: { label: string; value: string; hint?: string; children?: ReactNode }) {
  return (
    <Paper withBorder p="md">
      <Text size="xs" c="dimmed" tt="uppercase" fw={600}>
        {label}
      </Text>
      <Text fz={26} fw={700}>
        {value}
      </Text>
      {hint && (
        <Text size="xs" c="dimmed">
          {hint}
        </Text>
      )}
      {children}
    </Paper>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <Table.Tr>
      <Table.Td>
        <Text size="sm" c="dimmed">
          {label}
        </Text>
      </Table.Td>
      <Table.Td ta="right">
        <Text size="sm" fw={600}>
          {value}
        </Text>
      </Table.Td>
    </Table.Tr>
  )
}

function SpendChart({ days }: { days: Schemas['DayRow'][] }) {
  const scheme = useComputedColorScheme('light')
  const [asTable, setAsTable] = useState(false)
  return (
    <div>
      <Group justify="space-between" mb="sm">
        <Title order={6}>AI spend per day (USD)</Title>
        <Button size="xs" variant="subtle" onClick={() => setAsTable(!asTable)}>
          {asTable ? 'Show chart' : 'Show as table'}
        </Button>
      </Group>
      {days.length === 0 && (
        <Text size="sm" c="dimmed">
          No AI calls yet.
        </Text>
      )}
      {days.length > 0 && !asTable && (
        <BarChart
          h={220}
          data={days}
          dataKey="day"
          series={[{ name: 'cost_usd', label: 'Spend', color: SERIES_1[scheme] }]}
          valueFormatter={(value) => `$${value.toFixed(4)}`}
          gridAxis="y"
          tickLine="none"
          maxBarWidth={24}
          barProps={{ radius: [4, 4, 0, 0] }}
        />
      )}
      {days.length > 0 && asTable && (
        <Table>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Day</Table.Th>
              <Table.Th ta="right">Calls</Table.Th>
              <Table.Th ta="right">Cache hits</Table.Th>
              <Table.Th ta="right">Spend</Table.Th>
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {days.map((d) => (
              <Table.Tr key={d.day}>
                <Table.Td>{d.day}</Table.Td>
                <Table.Td ta="right">{d.calls}</Table.Td>
                <Table.Td ta="right">{d.cache_hits}</Table.Td>
                <Table.Td ta="right">{money(d.cost_usd, 6)}</Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </div>
  )
}

function SpendTable({ title, rows }: { title: string; rows: Schemas['SpendRow'][] }) {
  return (
    <div>
      <Title order={6} mb="sm">
        {title}
      </Title>
      <Table>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Name</Table.Th>
            <Table.Th ta="right">Calls</Table.Th>
            <Table.Th ta="right">Cache hits</Table.Th>
            <Table.Th ta="right">Refused</Table.Th>
            <Table.Th ta="right">Tokens</Table.Th>
            <Table.Th ta="right">Spend</Table.Th>
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {rows.map((row) => (
            <Table.Tr key={row.key}>
              <Table.Td>
                <Text size="sm">{row.key}</Text>
              </Table.Td>
              <Table.Td ta="right">{row.calls}</Table.Td>
              <Table.Td ta="right">{row.cache_hits}</Table.Td>
              <Table.Td ta="right">{row.refused}</Table.Td>
              <Table.Td ta="right">{(row.tokens_in + row.tokens_out).toLocaleString()}</Table.Td>
              <Table.Td ta="right">{money(row.cost_usd, 6)}</Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
    </div>
  )
}

function SettingsPanel() {
  const queryClient = useQueryClient()
  const settings = useQuery({ queryKey: ['admin', 'settings'], queryFn: () => unwrap(api.GET('/api/admin/settings')) })
  const [draft, setDraft] = useState<RuntimeSettings | null>(null)
  const save = useMutation({
    mutationFn: (body: RuntimeSettings) => unwrap(api.PUT('/api/admin/settings', { body })),
    onSuccess: (saved) => {
      queryClient.setQueryData(['admin', 'settings'], saved)
      queryClient.invalidateQueries({ queryKey: ['admin', 'costs'] })
      setDraft(null)
      notifications.show({ title: 'Saved', message: 'Takes effect on the next AI request.' })
    },
    onError: (error) => notifications.show({ color: 'red', title: 'Not saved', message: error.message }),
  })
  if (settings.isError) return <Alert color="red" title="Couldn't load the AI settings">{settings.error.message}</Alert>
  if (!settings.data) return <Loader />
  const current = draft ?? settings.data
  const update = (next: Partial<RuntimeSettings>) => setDraft({ ...current, ...next })
  const updateTask = (name: string, patch: Partial<RuntimeSettings['tasks'][string]>) =>
    update({ tasks: { ...current.tasks, [name]: { ...current.tasks[name], ...patch } } })

  return (
    <div>
      <Group justify="flex-end" mb="sm">
        <Button disabled={!draft} loading={save.isPending} onClick={() => draft && save.mutate(draft)}>
          Save changes
        </Button>
      </Group>
      <Stack gap="md">
        {Object.entries(current.tasks).map(([name, task]) => (
          <Group key={name} align="end" wrap="wrap">
            <Switch
              checked={task.enabled}
              onChange={(e) => updateTask(name, { enabled: e.currentTarget.checked })}
              label={<Text fw={600}>{name.replaceAll('_', ' ')}</Text>}
              description={task.description}
              w={420}
            />
            <TextInput
              label="Model (OpenRouter id)"
              value={task.model}
              onChange={(e) => updateTask(name, { model: e.currentTarget.value })}
              w={320}
            />
          </Group>
        ))}
        <NumberInput
          label="Daily AI budget (USD)"
          min={0}
          step={0.5}
          decimalScale={2}
          prefix="$"
          value={current.daily_budget_usd}
          onChange={(value) => update({ daily_budget_usd: Number(value) || 0 })}
          w={200}
        />
      </Stack>
    </div>
  )
}
