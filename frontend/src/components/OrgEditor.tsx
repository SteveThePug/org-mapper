// Admin form to add or edit a tracked organisation (the same fields as an entry in organisations.yaml).
// Adding starts with "Set up with AI": type a name or website, the backend reads the site and an
// AI fills in the form below. Nothing is saved until the admin checks it and clicks Save.
// "Test" runs a survey against the unsaved form so an admin can check the discovery settings
// find profiles, and see what extraction gets from one, before saving.
import {
  Alert,
  Anchor,
  Button,
  Code,
  Group,
  Loader,
  Modal,
  NumberInput,
  SegmentedControl,
  SimpleGrid,
  Stack,
  Switch,
  Table,
  TagsInput,
  Text,
  Textarea,
  TextInput,
  Title,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import { IconSparkles } from '@tabler/icons-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, unwrap, type Schemas } from '../api/client'
import { useMeta } from '../lib'

type OrgIn = Schemas['OrgIn']
type Survey = Schemas['SurveyOut']
type AiSetup = Schemas['AiSetupOut']

// Generic fields extraction can take a CSS selector for (see backend scraping/extract.py); the domain
// profile's extra fields (bar admissions, nationality...) are added from /api/meta.
const CORE_SELECTOR_FIELDS = ['name', 'title', 'offices', 'groups', 'education']

const EMPTY: OrgIn = { name: '', website: 'https://', kind: '', selectors: {}, enabled: true, notes: '' }

type SourceType = 'sitemap' | 'listing'

type Form = OrgIn & {
  source: SourceType
  urls: string[]
  include: string
  exclude: string
  child_include: string
  link_selector: string
  next_selector: string
}

// The form edits the nested `discovery` object as flat fields, whichever source type it is.
function toForm(org: OrgIn): Form {
  const d = org.discovery
  return {
    ...org,
    source: d?.type ?? 'sitemap',
    urls: d?.urls ?? [],
    include: d?.include ?? '',
    exclude: d?.exclude ?? '',
    child_include: d?.type === 'sitemap' ? (d.child_include ?? '') : '',
    link_selector: d?.type === 'listing' ? d.link_selector : '',
    next_selector: d?.type === 'listing' ? (d.next_selector ?? '') : '',
  }
}

function fromForm(form: Form): OrgIn {
  const { source, urls, include, exclude, child_include, link_selector, next_selector, ...org } = form
  const selectors = Object.fromEntries(Object.entries(org.selectors ?? {}).filter(([, selector]) => selector.trim()))
  // Discovery not filled in yet: the organisation is listed but not crawled.
  let discovery: OrgIn['discovery'] = null
  if (source === 'sitemap' && urls.length && include) {
    discovery = { type: 'sitemap', urls, include, exclude: exclude || null, child_include: child_include || null }
  } else if (source === 'listing' && urls.length && link_selector) {
    discovery = { type: 'listing', urls, link_selector, next_selector: next_selector || null, include, exclude: exclude || null }
  }
  return { ...org, selectors, discovery }
}

/** `orgId` undefined = create a new organisation. */
export function OrgEditor({ orgId, onClose }: { orgId?: string; onClose: () => void }) {
  const existing = useQuery({
    queryKey: ['admin', 'org', orgId],
    queryFn: () => unwrap(api.GET('/api/admin/orgs/{org_id}', { params: { path: { org_id: orgId! } } })),
    enabled: !!orgId,
  })
  // Adding: first ask for a name or website (or skip). undefined = still on that first step.
  const [start, setStart] = useState<{ initial: OrgIn; ai?: AiSetup } | undefined>(orgId ? { initial: EMPTY } : undefined)
  return (
    <Modal opened onClose={onClose} size="xl" title={orgId ? 'Edit organisation' : 'Add organisation'}>
      {orgId && existing.isPending ? (
        <Loader />
      ) : orgId && existing.isError ? (
        // Never fall back to a blank form when editing: saving it would overwrite the record with blanks.
        <Alert color="red" title="Couldn't load this organisation">
          {existing.error.message}
        </Alert>
      ) : !orgId && !start ? (
        <AiSetupStep onDone={(ai) => setStart({ initial: ai.suggestion, ai })} onSkip={() => setStart({ initial: EMPTY })} />
      ) : (
        <OrgForm orgId={orgId} initial={existing.data ?? start?.initial ?? EMPTY} ai={start?.ai} onClose={onClose} />
      )}
    </Modal>
  )
}

function useAiSetup(onSuccess: (ai: AiSetup) => void) {
  return useMutation({
    mutationFn: (query: string) => unwrap(api.POST('/api/admin/orgs/ai-setup', { body: { query } })),
    onSuccess,
    onError: (error: Error) => notifications.show({ color: 'red', title: 'AI setup failed', message: error.message }),
  })
}

/** First step when adding: a name or website in, a filled-in form out. */
function AiSetupStep({ onDone, onSkip }: { onDone: (ai: AiSetup) => void; onSkip: () => void }) {
  const [query, setQuery] = useState('')
  const setup = useAiSetup(onDone)
  const submit = () => query.trim().length >= 2 && setup.mutate(query.trim())
  return (
    <Stack>
      <Text size="sm">
        Type the organisation's name or website. We read the site (its sitemaps and a sample profile), and an AI fills in the scraper settings. You
        check them before saving.
      </Text>
      <TextInput
        label="Name or website"
        placeholder="e.g. Slaughter and May, or slaughterandmay.com"
        maxLength={200}
        value={query}
        onChange={(e) => setQuery(e.currentTarget.value)}
        onKeyDown={(e) => e.key === 'Enter' && submit()}
        disabled={setup.isPending}
        data-autofocus
      />
      {setup.isPending && (
        <Text size="sm" c="dimmed">
          Reading the website and asking the AI. This can take a minute or two: we visit the site politely, a few seconds per page.
        </Text>
      )}
      <Group justify="space-between">
        <Anchor component="button" size="sm" onClick={onSkip} disabled={setup.isPending}>
          Fill in manually instead
        </Anchor>
        <Button leftSection={<IconSparkles size={16} />} loading={setup.isPending} disabled={query.trim().length < 2} onClick={submit}>
          Set up with AI
        </Button>
      </Group>
    </Stack>
  )
}

function OrgForm({ orgId, initial, ai, onClose }: { orgId?: string; initial: OrgIn; ai?: AiSetup; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<Form>(() => toForm(initial))
  const [survey, setSurvey] = useState<Survey | null>(ai?.survey ?? null)
  const [aiResult, setAiResult] = useState<AiSetup | undefined>(ai)
  const set = (patch: Partial<Form>) => setForm((f) => ({ ...f, ...patch }))
  const selectorFields = [...CORE_SELECTOR_FIELDS, ...(useMeta().data?.fields.map((f) => f.key) ?? [])]
  const fail = (title: string) => (error: Error) => notifications.show({ color: 'red', title, message: error.message })

  const save = useMutation({
    mutationFn: () =>
      orgId
        ? unwrap(api.PUT('/api/admin/orgs/{org_id}', { params: { path: { org_id: orgId } }, body: fromForm(form) }))
        : unwrap(api.POST('/api/admin/orgs', { body: fromForm(form) })),
    onSuccess: (org) => {
      notifications.show({ title: 'Saved', message: `${org.name} is saved; the crawler picks it up within a minute.` })
      void queryClient.invalidateQueries({ queryKey: ['organisations'] })
      void queryClient.invalidateQueries({ queryKey: ['meta'] })
      onClose()
    },
    onError: fail('Not saved'),
  })
  const test = useMutation({
    mutationFn: () => unwrap(api.POST('/api/admin/orgs/survey', { body: fromForm(form) })),
    onSuccess: setSurvey,
    onError: fail('Survey failed'),
  })
  // "Ask AI again" on an existing form: keep the name and on/off switch, replace the scraper settings.
  const redo = useAiSetup((result) => {
    setForm((f) => toForm({ ...result.suggestion, name: f.name || result.suggestion.name, enabled: f.enabled, min_delay_s: f.min_delay_s }))
    setSurvey(result.survey)
    setAiResult(result)
  })
  const discover = useMutation({
    mutationFn: () => unwrap(api.POST('/api/admin/orgs/{org_id}/discover', { params: { path: { org_id: orgId! } } })),
    onSuccess: () => notifications.show({ title: 'Queued', message: 'Discovery runs when the worker is next free.' }),
    onError: fail('Not queued'),
  })

  return (
    <Stack>
      {aiResult && <AiNote ai={aiResult} />}
      <Group grow>
        <TextInput label="Name" required value={form.name} onChange={(e) => set({ name: e.currentTarget.value })} />
        <TextInput label="Kind" description="Free text, e.g. law_firm, bank, club" value={form.kind ?? ''} onChange={(e) => set({ kind: e.currentTarget.value })} />
      </Group>
      <TextInput label="Website" required value={form.website} onChange={(e) => set({ website: e.currentTarget.value })} />

      <Title order={6} mt="sm">
        Discovery: where the people pages are listed
      </Title>
      <SegmentedControl
        value={form.source}
        onChange={(value) => set({ source: value as SourceType })}
        data={[
          { value: 'sitemap', label: 'XML sitemap' },
          { value: 'listing', label: 'Listing page (HTML)' },
        ]}
      />
      {form.source === 'sitemap' ? (
        <TagsInput label="Sitemap URLs" description="Press Enter after each. A sitemap index is followed." value={form.urls} onChange={(urls) => set({ urls })} />
      ) : (
        <>
          <TagsInput label="Listing page URLs" description="Pages that link to every profile, e.g. /people or /squad. Press Enter after each." value={form.urls} onChange={(urls) => set({ urls })} />
          <Group grow>
            <TextInput label="Profile link selector" description="CSS selector of the <a> links to profiles" value={form.link_selector} onChange={(e) => set({ link_selector: e.currentTarget.value })} ff="monospace" />
            <TextInput label="Next page selector" description="Optional, for paginated listings" value={form.next_selector} onChange={(e) => set({ next_selector: e.currentTarget.value })} ff="monospace" />
          </Group>
        </>
      )}
      <TextInput
        label={form.source === 'sitemap' ? 'Profile URL regex (include)' : 'Profile URL regex (optional)'}
        description="A URL must match this to count as a person's profile"
        value={form.include}
        onChange={(e) => set({ include: e.currentTarget.value })}
        ff="monospace"
      />
      <Group grow>
        <TextInput label="Exclude regex" value={form.exclude} onChange={(e) => set({ exclude: e.currentTarget.value })} ff="monospace" />
        {form.source === 'sitemap' && (
          <TextInput
            label="Child sitemap regex"
            description="In a sitemap index, only follow these"
            value={form.child_include}
            onChange={(e) => set({ child_include: e.currentTarget.value })}
            ff="monospace"
          />
        )}
      </Group>

      <Title order={6} mt="sm">
        CSS selectors (only for fields the generic extraction misses)
      </Title>
      <SimpleGrid cols={{ base: 1, sm: 3 }}>
        {selectorFields.map((field) => (
          <SelectorInput key={field} field={field} form={form} set={set} />
        ))}
      </SimpleGrid>

      <Group grow align="start">
        <NumberInput
          label="Min delay between requests (s)"
          description="Blank = the global default"
          min={0}
          value={form.min_delay_s ?? ''}
          onChange={(v) => set({ min_delay_s: typeof v === 'number' ? v : null })}
        />
        <Switch mt={30} label="Enabled (crawled)" checked={form.enabled ?? true} onChange={(e) => set({ enabled: e.currentTarget.checked })} />
      </Group>
      <Textarea label="Notes" autosize minRows={2} value={form.notes ?? ''} onChange={(e) => set({ notes: e.currentTarget.value })} />

      {survey && <SurveyResult survey={survey} />}

      <Group justify="space-between">
        <Group>
          <Button variant="default" loading={test.isPending} onClick={() => test.mutate()}>
            Test (survey the site)
          </Button>
          <Button
            variant="default"
            leftSection={<IconSparkles size={16} />}
            loading={redo.isPending}
            disabled={!/^https?:\/\/[^/]+\.[^/]+/.test(form.website)}
            onClick={() => redo.mutate(form.website)}
          >
            {aiResult ? 'Ask AI again' : 'Fill in with AI'}
          </Button>
          {orgId && (
            <Button variant="default" loading={discover.isPending} onClick={() => discover.mutate()}>
              Run discovery now
            </Button>
          )}
        </Group>
        <Button loading={save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
      </Group>
    </Stack>
  )
}

function AiNote({ ai }: { ai: AiSetup }) {
  return (
    <Alert color="violet" icon={<IconSparkles size={16} />} title="Filled in by AI: please check before saving">
      <Stack gap={4}>
        <Text size="sm">{ai.suggestion.notes?.replace(/^Set up with AI:?\s*/, '') || 'No comment from the AI.'}</Text>
        {ai.warnings.map((warning) => (
          <Text key={warning} size="xs" c="orange">
            {warning}
          </Text>
        ))}
        <Text size="xs" c="dimmed">
          The test result at the bottom shows what these settings find. AI cost: ${ai.cost_usd.toFixed(4)}
        </Text>
      </Stack>
    </Alert>
  )
}

function SelectorInput({ field, form, set }: { field: string; form: Form; set: (patch: Partial<Form>) => void }) {
  return (
    <TextInput
      label={field}
      ff="monospace"
      value={form.selectors?.[field] ?? ''}
      onChange={(e) => set({ selectors: { ...form.selectors, [field]: e.currentTarget.value } })}
    />
  )
}

function verdictColor(verdict: string): string {
  if (verdict.startsWith('READY')) return 'teal'
  if (verdict.startsWith('PARTIAL')) return 'yellow'
  return 'red'
}

function SurveyResult({ survey }: { survey: Survey }) {
  return (
    <Alert color={verdictColor(survey.verdict)} title={survey.verdict}>
      <Stack gap={4}>
        <Text size="sm">robots.txt: {survey.robots}</Text>
        <Text size="sm">
          {survey.configured_count !== null
            ? `Your discovery settings found ${survey.configured_count} profile URLs.`
            : `${survey.profile_count} likely profile URLs under ${survey.profile_prefix ?? '-'}.`}
        </Text>
        {survey.configured_count === null && survey.suggested_include && (
          <Text size="sm">
            Suggested include regex: <Code>{survey.suggested_include}</Code>
          </Text>
        )}
        {survey.people_sitemaps.length > 0 && <Text size="sm">People sitemaps: {survey.people_sitemaps.slice(0, 4).join(', ')}</Text>}
        {survey.sample_url && (
          <>
            <Text size="sm">Sample: {survey.sample_url}</Text>
            <Table fz="xs" withRowBorders={false}>
              <Table.Tbody>
                {Object.entries(survey.sample_values).map(([key, value]) => (
                  <Table.Tr key={key}>
                    <Table.Td fw={600}>{key}</Table.Td>
                    <Table.Td c="dimmed">{survey.sample_fields[key] ?? ''}</Table.Td>
                    <Table.Td>{JSON.stringify(value).slice(0, 120)}</Table.Td>
                  </Table.Tr>
                ))}
              </Table.Tbody>
            </Table>
          </>
        )}
        {survey.errors.slice(0, 5).map((error) => (
          <Text key={error} size="xs" c="red">
            {error}
          </Text>
        ))}
      </Stack>
    </Alert>
  )
}
