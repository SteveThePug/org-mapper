// Admin page for the domain profile: who we track and what we call them (config/domains/*.yaml).
// Three tabs: segments (who counts), words (what the UI calls things) and rules (how a website's
// own wording maps to ours). Segments and words get proper form fields. The rules are lists of
// keywords and regexes, edited as JSON, with a read-only overview above the editor. The server
// validates everything on save (bad regex, a segment pointing at a group that doesn't exist...).
// Nothing is saved until the floating "Save" bar is used, so a half-finished edit can be discarded.
import {
  ActionIcon,
  Affix,
  Alert,
  Badge,
  Button,
  Card,
  Group,
  JsonInput,
  Loader,
  MultiSelect,
  Paper,
  SegmentedControl,
  SimpleGrid,
  Stack,
  Tabs,
  Text,
  Textarea,
  TextInput,
  ThemeIcon,
  Title,
  Tooltip,
  Transition,
} from '@mantine/core'
import { notifications } from '@mantine/notifications'
import {
  IconAbc,
  IconAppWindow,
  IconBraces,
  IconBuilding,
  IconCategory,
  IconListDetails,
  IconPlus,
  IconRefresh,
  IconRobot,
  IconStar,
  IconStarFilled,
  IconTag,
  IconTrash,
  IconTrendingUp,
  IconUsersGroup,
} from '@tabler/icons-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState, type ComponentType, type CSSProperties, type ReactNode } from 'react'
import { api, unwrap, type Schemas } from '../api/client'
import { PageTitle } from '../components/common'
import { formatDate, useMeta, useUrlFilters, useWords } from '../lib'

type Profile = Schemas['DomainConfig']
type Segment = Schemas['Segment']
type Vocabulary = Schemas['Vocabulary']
type Icon = ComponentType<{ size?: number; style?: CSSProperties }>
type Choice = { value: string; label: string }

// The rule parts of the profile, edited together as one JSON document.
type Rules = Pick<Profile, 'titles' | 'promotion_to' | 'taxonomy' | 'fields' | 'seniority'>

// The vocabulary comes in singular/plural pairs, shown as one row each.
const WORD_PAIRS: { label: string; one: keyof Vocabulary; many: keyof Vocabulary }[] = [
  { label: 'Person', one: 'person', many: 'people' },
  { label: 'Organisation', one: 'org', many: 'orgs' },
  { label: 'Group', one: 'group', many: 'groups' },
  { label: 'Location', one: 'location', many: 'locations' },
]

function rulesOf(profile: Profile): Rules {
  const { titles, promotion_to, taxonomy, fields, seniority } = profile
  return { titles, promotion_to, taxonomy, fields, seniority }
}

const asJson = (value: unknown) => JSON.stringify(value, null, 2)

/** The rules as typed, or null while the text isn't valid JSON. */
function parseRules(text: string): Rules | null {
  try {
    return JSON.parse(text) as Rules
  } catch {
    return null
  }
}

export function TrackingPage() {
  const domain = useQuery({ queryKey: ['admin', 'domain'], queryFn: () => unwrap(api.GET('/api/admin/domain')) })
  if (domain.isPending) return <Loader />
  if (domain.error) return <Alert color="red">{domain.error.message}</Alert>
  // Keyed on the save time, so the form starts again from what the server stored after each save.
  return <TrackingForm key={domain.data.updated_at ?? 'seed'} stored={domain.data} />
}

function TrackingForm({ stored }: { stored: Schemas['DomainOut'] }) {
  const queryClient = useQueryClient()
  const words = useWords()
  const orgs = useMeta().data?.organisations ?? []
  const { values, update } = useUrlFilters({ tab: 'segments' })
  const [profile, setProfile] = useState<Profile>(stored.profile)
  const [rules, setRules] = useState(() => asJson(rulesOf(stored.profile)))
  const set = (patch: Partial<Profile>) => setProfile((p) => ({ ...p, ...patch }))

  // Choices for the segment editor come from the rules as currently typed (if they parse).
  const parsed = parseRules(rules)
  const current = parsed ?? rulesOf(profile)
  const titleChoices = Object.keys(current.titles ?? {})
  const groupChoices = Object.entries(current.taxonomy ?? {}).map(([value, tag]) => ({ value, label: tag.label }))
  const orgChoices = orgs.map((o) => ({ value: o.id, label: o.name }))

  // Compare parsed values, not text, so re-formatting the JSON doesn't count as a change.
  const dirty =
    asJson(profile) !== asJson(stored.profile) || parsed === null || asJson(parsed) !== asJson(rulesOf(stored.profile))
  const discard = () => {
    setProfile(stored.profile)
    setRules(asJson(rulesOf(stored.profile)))
  }

  const reprocess = useMutation({
    mutationFn: () => unwrap(api.POST('/api/admin/reprocess')),
    onSuccess: (result) =>
      notifications.show({
        title: 'Re-sorting everyone',
        message: `${result.queued} ${words.orgs.toLowerCase()} queued. Uses pages we already have; the worker must be running.`,
      }),
    onError: (error) => notifications.show({ color: 'red', title: 'Not queued', message: error.message }),
  })
  const save = useMutation({
    mutationFn: () => {
      if (parsed === null) throw new Error('The rules are not valid JSON')
      return unwrap(api.PUT('/api/admin/domain', { body: { ...profile, ...parsed } }))
    },
    onSuccess: (saved) => {
      queryClient.setQueryData(['admin', 'domain'], saved)
      void queryClient.invalidateQueries({ queryKey: ['meta'] })
      // New rules only apply to pages read from now on, so offer the follow-up step right in the toast.
      notifications.show({
        title: 'Saved',
        autoClose: 12_000,
        message: (
          <Stack gap={6} align="start">
            <Text size="sm">Rule changes apply to pages read from now on.</Text>
            <Button size="xs" variant="light" onClick={() => reprocess.mutate()}>
              Apply rules to everyone now
            </Button>
          </Stack>
        ),
      })
    },
    onError: (error) => notifications.show({ color: 'red', title: 'Not saved', message: error.message, autoClose: false }),
  })

  const setSegment = (index: number, patch: Partial<Segment>) => {
    const old = profile.segments[index]
    set({
      segments: profile.segments.map((s, i) => (i === index ? { ...s, ...patch } : s)),
      // Renaming the default segment's id keeps it the default.
      ...(patch.id !== undefined && profile.default_segment === old.id ? { default_segment: patch.id } : {}),
    })
  }
  const removeSegment = (index: number) => {
    const removed = profile.segments[index]
    set({
      segments: profile.segments.filter((_, i) => i !== index),
      ...(profile.default_segment === removed.id ? { default_segment: null } : {}),
    })
  }
  const addSegment = () =>
    set({
      segments: [
        ...profile.segments,
        { id: `segment-${profile.segments.length + 1}`, name: 'New segment', organisations: 'all', titles: [], groups: 'all' },
      ],
    })

  return (
    <Stack gap="lg" pb={dirty ? 80 : 0}>
      <Group justify="space-between" align="start" gap="md">
        <div>
          <PageTitle title="What we track" />
          <Text size="sm" c="dimmed" mt={4}>
            Profile <Text span ff="monospace" size="sm">{stored.name}</Text>
            {' · '}
            {stored.managed
              ? `edited by ${stored.updated_by ?? 'an admin'}, ${formatDate(stored.updated_at)}`
              : `from config/domains/${stored.name}.yaml`}
          </Text>
        </div>
        <Tooltip label="Re-sort everyone with the current rules, using pages we already have" multiline w={260}>
          <Button
            variant="default"
            leftSection={<IconRefresh size={16} />}
            loading={reprocess.isPending}
            onClick={() => reprocess.mutate()}
          >
            Apply rules to everyone
          </Button>
        </Tooltip>
      </Group>

      <Tabs value={values.tab} onChange={(tab) => update({ tab })} keepMounted={false}>
        <Tabs.List mb="lg">
          <Tabs.Tab value="segments" leftSection={<IconUsersGroup size={16} />}>
            Segments{' '}
            <Text span size="sm" c="dimmed">
              {profile.segments.length}
            </Text>
          </Tabs.Tab>
          <Tabs.Tab value="words" leftSection={<IconAbc size={16} />}>
            Words
          </Tabs.Tab>
          <Tabs.Tab value="rules" leftSection={<IconBraces size={16} />} color={parsed === null ? 'red' : undefined}>
            Rules
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="segments">
          <Hint>
            A segment is a group of {words.people.toLowerCase()} we follow. Someone is in a segment when they match all
            three rules. The default segment is the one the app shows first.
          </Hint>
          <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
            {profile.segments.map((segment, index) => (
              <SegmentCard
                key={index}
                segment={segment}
                isDefault={profile.default_segment === segment.id}
                titleChoices={titleChoices}
                groupChoices={groupChoices}
                orgChoices={orgChoices}
                onChange={(patch) => setSegment(index, patch)}
                onMakeDefault={() => set({ default_segment: segment.id })}
                onRemove={() => removeSegment(index)}
              />
            ))}
          </SimpleGrid>
          <Button variant="default" leftSection={<IconPlus size={16} />} mt="md" onClick={addSegment}>
            Add segment
          </Button>
        </Tabs.Panel>

        <Tabs.Panel value="words">
          <Hint>What the app calls things, and who the AI writes for. Use the words people in your field would use.</Hint>
          <WordsPanel profile={profile} set={set} />
        </Tabs.Panel>

        <Tabs.Panel value="rules">
          <Hint>
            How a website's own wording maps to ours: which job titles count as which title, which keywords put someone in
            which {words.group.toLowerCase()}, and any extra details to pick up. The summary updates as you type.
          </Hint>
          <Stack>
            <RulesOverview rules={current} />
            <Section
              icon={IconBraces}
              title="Edit as JSON"
              aside={
                parsed === null && (
                  <Badge color="red" variant="light">
                    Not valid JSON
                  </Badge>
                )
              }
            >
              <JsonInput
                value={rules}
                onChange={setRules}
                autosize
                minRows={12}
                maxRows={24}
                formatOnBlur
                validationError="Not valid JSON"
                ff="monospace"
                aria-label="Rules as JSON"
              />
            </Section>
          </Stack>
        </Tabs.Panel>
      </Tabs>

      <Affix position={{ bottom: 20, left: 0, right: 0 }} zIndex={150} style={{ pointerEvents: 'none' }}>
        <Transition transition="slide-up" mounted={dirty}>
          {(styles) => (
            <Group justify="center" style={styles}>
              <Paper shadow="lg" radius="xl" withBorder px="lg" py="xs" style={{ pointerEvents: 'auto' }}>
                <Group gap="md" wrap="nowrap">
                  <Text size="sm" fw={600}>
                    Unsaved changes
                  </Text>
                  <Button variant="subtle" color="gray" size="xs" onClick={discard}>
                    Discard
                  </Button>
                  <Button size="xs" radius="xl" loading={save.isPending} disabled={parsed === null} onClick={() => save.mutate()}>
                    Save
                  </Button>
                </Group>
              </Paper>
            </Group>
          )}
        </Transition>
      </Affix>
    </Stack>
  )
}

/** One line of dimmed help text at the top of a tab. */
function Hint({ children }: { children: ReactNode }) {
  return (
    <Text size="sm" c="dimmed" maw={720} mb="md">
      {children}
    </Text>
  )
}

/** A bordered card with an icon and title; `aside` sits on the right of the title row. */
function Section({ icon: Icon, title, aside, children }: { icon: Icon; title: string; aside?: ReactNode; children: ReactNode }) {
  return (
    <Card withBorder radius="md" padding="lg">
      <Group justify="space-between" mb="md" wrap="nowrap">
        <Group gap="sm" wrap="nowrap">
          <ThemeIcon variant="light" radius="md">
            <Icon size={16} />
          </ThemeIcon>
          <Title order={5}>{title}</Title>
        </Group>
        {aside}
      </Group>
      <Stack gap="sm">{children}</Stack>
    </Card>
  )
}

/** One segment: a name and id, then three "who counts" rules (titles, groups, organisations). */
function SegmentCard({
  segment,
  isDefault,
  titleChoices,
  groupChoices,
  orgChoices,
  onChange,
  onMakeDefault,
  onRemove,
}: {
  segment: Segment
  isDefault: boolean
  titleChoices: string[]
  groupChoices: Choice[]
  orgChoices: Choice[]
  onChange: (patch: Partial<Segment>) => void
  onMakeDefault: () => void
  onRemove: () => void
}) {
  const words = useWords()
  return (
    <Card withBorder radius="md" padding="lg">
      <Group justify="space-between" wrap="nowrap" gap="xs">
        <TextInput
          variant="unstyled"
          size="lg"
          value={segment.name}
          onChange={(e) => onChange({ name: e.currentTarget.value })}
          aria-label="Segment name"
          style={{ flex: 1 }}
          styles={{ input: { fontWeight: 700 } }}
        />
        <Group gap={4} wrap="nowrap">
          {isDefault ? (
            <Badge variant="light" leftSection={<IconStarFilled size={11} />}>
              Default
            </Badge>
          ) : (
            <Tooltip label="Make default">
              <ActionIcon variant="subtle" color="gray" onClick={onMakeDefault} aria-label="Make default">
                <IconStar size={16} />
              </ActionIcon>
            </Tooltip>
          )}
          <Tooltip label="Remove segment">
            <ActionIcon variant="subtle" color="gray" onClick={onRemove} aria-label="Remove segment">
              <IconTrash size={16} />
            </ActionIcon>
          </Tooltip>
        </Group>
      </Group>
      <Group gap={6} mb="md" wrap="nowrap">
        <Text size="xs" c="dimmed">
          ID
        </Text>
        <TextInput
          size="xs"
          variant="unstyled"
          value={segment.id}
          onChange={(e) => onChange({ id: e.currentTarget.value })}
          ff="monospace"
          aria-label="Segment id"
          style={{ flex: 1 }}
          styles={{ input: { color: 'var(--mantine-color-dimmed)' } }}
        />
      </Group>

      <Stack gap="sm">
        <RuleRow icon={IconTag} label="Titles">
          <MultiSelect
            data={titleChoices}
            value={segment.titles}
            onChange={(titles) => onChange({ titles })}
            placeholder={segment.titles.length ? undefined : 'Pick titles'}
          />
        </RuleRow>
        <RuleRow icon={IconCategory} label={words.groups}>
          <AnyOrSome value={segment.groups} data={groupChoices} onChange={(groups) => onChange({ groups })} />
        </RuleRow>
        <RuleRow icon={IconBuilding} label={words.orgs}>
          <AnyOrSome
            value={segment.organisations ?? 'all'}
            data={orgChoices}
            onChange={(organisations) => onChange({ organisations })}
          />
        </RuleRow>
      </Stack>
    </Card>
  )
}

function RuleRow({ icon: Icon, label, children }: { icon: Icon; label: string; children: ReactNode }) {
  return (
    <Group align="start" wrap="nowrap" gap="sm">
      <Group gap={6} w={{ base: 104, sm: 130 }} pt={6} wrap="nowrap" style={{ flexShrink: 0 }}>
        <Icon size={16} style={{ opacity: 0.6 }} />
        <Text size="sm" fw={500} truncate>
          {label}
        </Text>
      </Group>
      <div style={{ flex: 1, minWidth: 0 }}>{children}</div>
    </Group>
  )
}

/** "Any" or a chosen list: a toggle, with the picker shown only when choosing. */
function AnyOrSome({
  value,
  data,
  onChange,
}: {
  value: string[] | 'all'
  data: Choice[]
  onChange: (value: string[] | 'all') => void
}) {
  const any = value === 'all'
  return (
    <Stack gap={6}>
      <SegmentedControl
        size="xs"
        w={160}
        data={[
          { value: 'any', label: 'Any' },
          { value: 'some', label: 'Choose' },
        ]}
        value={any ? 'any' : 'some'}
        onChange={(mode) => onChange(mode === 'any' ? 'all' : [])}
      />
      {!any && (
        <MultiSelect data={data} value={value} onChange={onChange} searchable placeholder={value.length ? undefined : 'Pick one or more'} />
      )}
    </Stack>
  )
}

function WordsPanel({ profile, set }: { profile: Profile; set: (patch: Partial<Profile>) => void }) {
  const vocabulary = (profile.vocabulary ?? {}) as Vocabulary
  const setWord = (key: keyof Vocabulary, value: string) => set({ vocabulary: { ...vocabulary, [key]: value } })
  return (
    // Cards keep their own height (alignItems), so a short card doesn't stretch to match its neighbour.
    <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md" style={{ alignItems: 'start' }}>
      <Stack gap="md">
        <Section icon={IconAppWindow} title="App">
          <TextInput label="Name" value={vocabulary.app_name ?? ''} onChange={(e) => setWord('app_name', e.currentTarget.value)} />
          <TextInput label="Tagline" value={vocabulary.tagline ?? ''} onChange={(e) => setWord('tagline', e.currentTarget.value)} />
        </Section>
        <Section icon={IconAbc} title="Names">
          <Group gap="sm" wrap="nowrap">
            <div style={{ width: 110, flexShrink: 0 }} />
            <Text size="xs" c="dimmed" fw={600} style={{ flex: 1 }}>
              One
            </Text>
            <Text size="xs" c="dimmed" fw={600} style={{ flex: 1 }}>
              Many
            </Text>
          </Group>
          {WORD_PAIRS.map((pair) => (
            <Group key={pair.label} gap="sm" wrap="nowrap">
              <Text size="sm" fw={500} w={110} style={{ flexShrink: 0 }}>
                {pair.label}
              </Text>
              <TextInput
                style={{ flex: 1 }}
                value={vocabulary[pair.one] ?? ''}
                onChange={(e) => setWord(pair.one, e.currentTarget.value)}
                aria-label={`${pair.label}, one`}
              />
              <TextInput
                style={{ flex: 1 }}
                value={vocabulary[pair.many] ?? ''}
                onChange={(e) => setWord(pair.many, e.currentTarget.value)}
                aria-label={`${pair.label}, many`}
              />
            </Group>
          ))}
        </Section>
      </Stack>
      <Section icon={IconRobot} title="AI audience">
        <Text size="xs" c="dimmed" mt={-8}>
          Tells the AI who it is writing for, so the tone and detail fit.
        </Text>
        <Textarea
          label="Briefs"
          autosize
          minRows={3}
          value={profile.brief_context}
          onChange={(e) => set({ brief_context: e.currentTarget.value })}
        />
        <Textarea
          label="Digests"
          autosize
          minRows={3}
          value={profile.digest_context}
          onChange={(e) => set({ digest_context: e.currentTarget.value })}
        />
      </Section>
    </SimpleGrid>
  )
}

/** A read-only summary of the rules as currently typed, so the JSON below is easier to follow. */
function RulesOverview({ rules }: { rules: Rules }) {
  const words = useWords()
  const titles = Object.keys(rules.titles ?? {})
  const promotions = new Set(rules.promotion_to ?? [])
  const groups = Object.values(rules.taxonomy ?? {})
  const fields = rules.fields ?? []
  return (
    <SimpleGrid cols={{ base: 1, sm: 2, lg: 4 }} spacing="md">
      <Summary icon={IconTag} title="Titles" count={titles.length} note={promotions.size ? 'Highlighted: reaching it counts as a promotion' : undefined}>
        {titles.map((title) => (
          <Tag key={title} highlight={promotions.has(title)}>
            {title}
          </Tag>
        ))}
      </Summary>
      <Summary icon={IconCategory} title={words.groups} count={groups.length}>
        {groups.map((group) => (
          <Tag key={group.label}>{group.label}</Tag>
        ))}
      </Summary>
      <Summary icon={IconListDetails} title="Extra fields" count={fields.length}>
        {fields.map((field) => (
          <Tag key={field.key}>{field.label}</Tag>
        ))}
      </Summary>
      <Summary icon={IconTrendingUp} title="Seniority">
        {rules.seniority ? <Tag>{rules.seniority.label}</Tag> : <Text size="sm" c="dimmed">Off</Text>}
      </Summary>
    </SimpleGrid>
  )
}

/** A small value label. Not upper-cased: these are names typed by people, so they're shown as typed. */
function Tag({ highlight = false, children }: { highlight?: boolean; children: ReactNode }) {
  return (
    <Badge
      variant={highlight ? 'light' : 'default'}
      tt="none"
      fw={500}
      leftSection={highlight ? <IconTrendingUp size={11} /> : undefined}
    >
      {children}
    </Badge>
  )
}

function Summary({
  icon: Icon,
  title,
  count,
  note,
  children,
}: {
  icon: Icon
  title: string
  count?: number
  note?: string
  children: ReactNode
}) {
  return (
    <Card withBorder radius="md" padding="md">
      <Group justify="space-between" mb="sm" wrap="nowrap">
        <Group gap="xs" wrap="nowrap">
          <Icon size={16} style={{ opacity: 0.6 }} />
          <Text fw={600} size="sm">
            {title}
          </Text>
        </Group>
        {count !== undefined && (
          <Text size="sm" c="dimmed">
            {count}
          </Text>
        )}
      </Group>
      <Group gap={6}>{children}</Group>
      {note && (
        <Text size="xs" c="dimmed" mt="sm">
          {note}
        </Text>
      )}
    </Card>
  )
}
