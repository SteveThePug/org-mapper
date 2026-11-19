import { ActionIcon, Alert, Anchor, Badge, Button, Drawer, Group, Indicator, Loader, Pagination, Select, Stack, Table, Text, TextInput, Tooltip } from '@mantine/core'
import { useDebouncedCallback } from '@mantine/hooks'
import { IconDownload, IconFilterOff, IconNotes, IconSearch, IconUserSearch } from '@tabler/icons-react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router'
import { api, unwrap, type PersonSummary } from '../api/client'
import { EmptyState, Freshness, PageTitle } from '../components/common'
import { NotesPanel } from '../components/Notes'
import { useMeta, useUrlFilters, useWords } from '../lib'

const PAGE_SIZE = 50

type Filters = Record<string, string | null | undefined>

function exportUrl(filters: Filters): string {
  const setFilters = Object.entries(filters).filter((entry): entry is [string, string] => typeof entry[1] === 'string')
  return `/api/people/export.csv?${new URLSearchParams(setFilters)}`
}

export function DirectoryPage() {
  const meta = useMeta()
  const words = useWords()
  const navigate = useNavigate()
  const seniority = meta.data?.seniority
  // Filters live in the URL, so Back from a person's page returns to the same list.
  // segment '' = the domain profile's default segment ("default" asks the API to pick it before meta loads).
  const { values, update, isFiltered, clear } = useUrlFilters({
    q: '',
    segment: '',
    organisation: '',
    office: '',
    group: '',
    status: 'active',
    page: '1',
  })
  const segment = values.segment || meta.data?.default_segment || 'default'
  const page = Number(values.page) || 1

  // The text box updates instantly; the URL (and so the search) follows 300ms after typing stops.
  const [q, setQ] = useState(values.q)
  // The person whose notes are open in the side drawer, if any.
  const [notesFor, setNotesFor] = useState<PersonSummary | null>(null)
  const pushSearch = useDebouncedCallback((text: string) => update({ q: text, page: null }), 300)

  const filters = {
    q: values.q || undefined,
    segment,
    organisation: values.organisation || null,
    office: values.office || null,
    group: values.group || null,
    status: values.status,
  }
  const people = useQuery({
    queryKey: ['people', filters, page],
    queryFn: () => unwrap(api.GET('/api/people', { params: { query: { ...filters, page, page_size: PAGE_SIZE } } })),
    placeholderData: keepPreviousData,
  })
  // Changing any filter goes back to the first page of results.
  const setFilter = (key: 'segment' | 'organisation' | 'office' | 'group' | 'status') => (value: string | null) =>
    update({ [key]: value, page: null })

  const segments = [...(meta.data?.segments.map((s) => ({ value: s.id, label: s.name })) ?? []), { value: 'all', label: `Everyone we've found` }]

  return (
    <Stack>
      <PageTitle title={words.people} />
      <TextInput
        placeholder={`Search by name, e.g. "Smith"`}
        leftSection={<IconSearch size={16} />}
        value={q}
        onChange={(e) => {
          setQ(e.currentTarget.value)
          pushSearch(e.currentTarget.value)
        }}
        maw={480}
        aria-label="Search by name"
      />
      <Group align="end" wrap="wrap">
        <Select
          label="Showing"
          data={segments}
          value={segment}
          onChange={(v) => setFilter('segment')(v === meta.data?.default_segment ? null : v)}
          allowDeselect={false}
          w={230}
        />
        <Select label={words.org} placeholder="All" clearable searchable data={meta.data?.organisations.map((f) => ({ value: f.id, label: f.name })) ?? []} value={values.organisation || null} onChange={setFilter('organisation')} w={210} />
        <Select label={words.location} placeholder="All" clearable searchable data={meta.data?.offices ?? []} value={values.office || null} onChange={setFilter('office')} w={170} />
        <Select label={words.group} placeholder="All" clearable data={meta.data?.groups.map((g) => ({ value: g.id, label: g.name })) ?? []} value={values.group || null} onChange={setFilter('group')} w={190} />
        <Select
          label="Status"
          data={[
            { value: 'active', label: 'Still there' },
            { value: 'departed', label: 'Has left' },
          ]}
          value={values.status}
          onChange={(v) => setFilter('status')(v ?? 'active')}
          allowDeselect={false}
          w={140}
        />
        {isFiltered && (
          <Button
            variant="subtle"
            color="gray"
            leftSection={<IconFilterOff size={16} />}
            onClick={() => {
              setQ('')
              clear()
            }}
          >
            Clear filters
          </Button>
        )}
      </Group>

      {people.error && <Alert color="red">{people.error.message}</Alert>}
      <Group justify="space-between">
        <Text size="sm" c="dimmed">
          {people.data ? <ResultSentence total={people.data.total} filters={filters} segmentName={segments.find((s) => s.value === segment)?.label} /> : 'Loading…'}
        </Text>
        <Button component="a" href={exportUrl(filters)} variant="default" size="xs" leftSection={<IconDownload size={14} />}>
          Download as spreadsheet
        </Button>
      </Group>

      {people.data?.total === 0 ? (
        <EmptyState
          icon={IconUserSearch}
          title={`No ${words.people.toLowerCase()} match`}
          action={isFiltered ? 'Clear filters' : undefined}
          onAction={() => {
            setQ('')
            clear()
          }}
        />
      ) : (
        <Table.ScrollContainer minWidth={900}>
          <Table striped highlightOnHover verticalSpacing="xs">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Name</Table.Th>
                <Table.Th>{words.org}</Table.Th>
                <Table.Th>Title</Table.Th>
                <Table.Th>{words.location}</Table.Th>
                <Table.Th>{words.groups}</Table.Th>
                {seniority && <Table.Th>{seniority.label}</Table.Th>}
                <Table.Th>Last checked</Table.Th>
                <Table.Th>Notes</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {people.data?.items.map((p) => (
                // The whole row opens the person; the name stays a real link (middle-click, new tab).
                <Table.Tr key={p.id} onClick={() => navigate(`/people/${p.id}`)} style={{ cursor: 'pointer' }}>
                  <Table.Td>
                    <Anchor component={Link} to={`/people/${p.id}`} fw={600} size="sm" onClick={(e) => e.stopPropagation()}>
                      {p.name ?? 'Unknown'}
                    </Anchor>
                    {!p.baseline && (
                      <Badge size="xs" ml={6} variant="light" color="teal">
                        joined recently
                      </Badge>
                    )}
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{p.org_name}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{p.title ?? '-'}</Text>
                  </Table.Td>
                  <Table.Td>
                    <Text size="sm">{p.offices.join(', ') || '-'}</Text>
                  </Table.Td>
                  <Table.Td maw={320}>
                    <Text size="sm" lineClamp={2}>
                      {p.groups_raw.join(', ') || '-'}
                    </Text>
                  </Table.Td>
                  {seniority && (
                    <Table.Td>
                      <Text size="sm">{p.seniority_year ?? '-'}</Text>
                    </Table.Td>
                  )}
                  <Table.Td>
                    <Freshness value={p.last_fetched} />
                  </Table.Td>
                  <Table.Td>
                    <Tooltip label={p.note_count ? `Read or add notes (${p.note_count})` : 'Add a note'}>
                      <Indicator label={p.note_count} size={16} disabled={!p.note_count} offset={4}>
                        <ActionIcon
                          variant={p.note_count ? 'light' : 'subtle'}
                          aria-label={`Notes on ${p.name ?? 'this person'}`}
                          onClick={(e) => {
                            e.stopPropagation() // don't also open the profile
                            setNotesFor(p)
                          }}
                        >
                          <IconNotes size={16} />
                        </ActionIcon>
                      </Indicator>
                    </Tooltip>
                  </Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>
      )}
      {people.data && people.data.total > PAGE_SIZE && (
        <Pagination
          total={Math.ceil(people.data.total / PAGE_SIZE)}
          value={page}
          onChange={(next) => {
            update({ page: String(next) })
            window.scrollTo({ top: 0 })
          }}
        />
      )}
      <Drawer opened={notesFor !== null} onClose={() => setNotesFor(null)} position="right" title={notesFor && <NotesDrawerTitle person={notesFor} />}>
        {notesFor && <DrawerNotes personId={notesFor.id} />}
      </Drawer>
    </Stack>
  )
}

function NotesDrawerTitle({ person }: { person: PersonSummary }) {
  return (
    <div>
      <Text fw={700}>Team notes: {person.name ?? 'Unknown'}</Text>
      <Text size="sm" c="dimmed">
        {person.org_name} ·{' '}
        <Anchor component={Link} to={`/people/${person.id}`} inherit>
          Open profile
        </Anchor>
      </Text>
    </div>
  )
}

/** The notes alone, so the drawer doesn't load the whole profile (and its AI summary). */
function DrawerNotes({ personId }: { personId: string }) {
  const notes = useQuery({
    queryKey: ['notes', personId],
    queryFn: () => unwrap(api.GET('/api/people/{person_id}/notes', { params: { path: { person_id: personId } } })),
  })
  if (notes.isPending) return <Loader size="sm" />
  if (notes.error) return <Alert color="red">{notes.error.message}</Alert>
  return <NotesPanel personId={personId} notes={notes.data} />
}

/** "Showing 312 people still at Clifford Chance in London, named like "smith"" - the filters as a sentence. */
function ResultSentence({ total, filters, segmentName }: { total: number; filters: Filters; segmentName?: string }) {
  const meta = useMeta()
  const words = useWords()
  const organisation = meta.data?.organisations.find((f) => f.id === filters.organisation)?.name
  const group = meta.data?.groups.find((g) => g.id === filters.group)?.name
  const noun = total === 1 ? words.person.toLowerCase() : words.people.toLowerCase()
  const parts = [
    `Showing ${total.toLocaleString()} ${noun}`,
    segmentName && filters.segment !== 'all' ? `(${segmentName})` : '',
    filters.status === 'departed' ? 'who have left' : 'still there',
    organisation ? `at ${organisation}` : '',
    filters.office ? `in ${filters.office}` : '',
    group ? `in ${group}` : '',
    filters.q ? `named like "${filters.q}"` : '',
  ]
  return <>{parts.filter(Boolean).join(' ')}</>
}
