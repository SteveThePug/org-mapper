import { ActionIcon, Alert, Anchor, Badge, Button, Group, Loader, Progress, Select, Stack, Switch, Table, Text, Tooltip } from '@mantine/core'
import {
  IconAlertTriangle,
  IconBan,
  IconCircleCheck,
  IconClockPause,
  IconExternalLink,
  IconPencil,
  IconPlus,
  IconTool,
} from '@tabler/icons-react'
import { useQuery } from '@tanstack/react-query'
import { lazy, Suspense, useState, type ComponentType } from 'react'
import { Link } from 'react-router'
import { api, unwrap, type OrganisationOut } from '../api/client'
import { Freshness, PageTitle } from '../components/common'
import { useUrlFilters, useWords, type Vocabulary } from '../lib'

// Each status in everyday words, with a one-line explanation in the tooltip.
function statusStyle(words: Vocabulary): Record<string, { label: string; help: string; color: string; icon: ComponentType<{ size?: number }> }> {
  const org = words.org.toLowerCase()
  return {
    ok: { label: 'Up to date', help: `We're reading this ${org}'s website without problems.`, color: 'teal', icon: IconCircleCheck },
    new: { label: 'Not checked yet', help: `Just added. The first visit will happen soon.`, color: 'gray', icon: IconClockPause },
    degraded: { label: 'Having trouble', help: `Some pages couldn't be read, so details may be missing or out of date.`, color: 'orange', icon: IconAlertTriangle },
    blocked: { label: "Site won't let us in", help: `The website refuses automated visits, so we don't track it.`, color: 'red', icon: IconBan },
    not_configured: { label: 'Not set up yet', help: `An admin still needs to tell us where this ${org} lists its people.`, color: 'gray', icon: IconTool },
  }
}

// The editor is admin-only, so it's loaded the first time an admin opens it rather than for everyone.
const OrgEditor = lazy(() => import('../components/OrgEditor').then((m) => ({ default: m.OrgEditor })))

const pct = (value: number | undefined) => (value === undefined ? '-' : `${Math.round(value * 100)}%`)

export function OrganisationsPage({ isAdmin }: { isAdmin: boolean }) {
  const organisations = useQuery({ queryKey: ['organisations'], queryFn: () => unwrap(api.GET('/api/orgs')), refetchInterval: 30_000 })
  const { values, update } = useUrlFilters({ kind: '', details: '' })
  // The crawler numbers are for admins diagnosing a site; everyone else sees the simple table.
  const showDetails = isAdmin && values.details === '1'
  const words = useWords()
  // undefined = editor closed, null = adding a new organisation, string = editing that id
  const [editing, setEditing] = useState<string | null | undefined>(undefined)
  if (organisations.isPending) return <Loader />
  if (organisations.error) return <Alert color="red">{organisations.error.message}</Alert>
  const kinds = [...new Set(organisations.data.map((f) => f.kind))].sort()
  const shown = organisations.data.filter((f) => !values.kind || f.kind === values.kind)

  return (
    <Stack>
      <PageTitle title={words.orgs} />
      <Group justify="space-between">
        <Group>
          {kinds.length > 1 && (
            <Select
              placeholder="All types"
              data={kinds.map((k) => ({ value: k, label: k.replaceAll('_', ' ') }))}
              value={values.kind || null}
              onChange={(kind) => update({ kind })}
              clearable
              w={220}
              aria-label="Filter by type"
            />
          )}
          {isAdmin && (
            <Switch
              label="Show technical details"
              checked={showDetails}
              onChange={(e) => update({ details: e.currentTarget.checked ? '1' : null })}
            />
          )}
        </Group>
        {isAdmin && (
          <Button leftSection={<IconPlus size={16} />} onClick={() => setEditing(null)}>
            Add {words.org.toLowerCase()}
          </Button>
        )}
      </Group>
      {editing !== undefined && (
        <Suspense fallback={<Loader />}>
          <OrgEditor orgId={editing ?? undefined} onClose={() => setEditing(undefined)} />
        </Suspense>
      )}
      <Table.ScrollContainer minWidth={showDetails ? 1000 : 640}>
        <Table verticalSpacing="sm" striped highlightOnHover>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>{words.org}</Table.Th>
              <Table.Th>Status</Table.Th>
              <Table.Th ta="right">{words.people} tracked</Table.Th>
              <Table.Th ta="right">Have left</Table.Th>
              <Table.Th>Last checked</Table.Th>
              {showDetails && (
                <>
                  <Table.Th ta="right">
                    <Tooltip label="Profile pages found / read so far / waiting to be read">
                      <span>Found / read / queued</span>
                    </Tooltip>
                  </Table.Th>
                  <Table.Th>Data quality</Table.Th>
                </>
              )}
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {shown.map((organisation) => (
              <OrganisationRow key={organisation.id} organisation={organisation} showDetails={showDetails} onEdit={isAdmin ? () => setEditing(organisation.id) : undefined} />
            ))}
          </Table.Tbody>
        </Table>
      </Table.ScrollContainer>
    </Stack>
  )
}

function OrganisationRow({ organisation, showDetails, onEdit }: { organisation: OrganisationOut; showDetails: boolean; onEdit?: () => void }) {
  const words = useWords()
  const styles = statusStyle(words)
  const status = styles[organisation.status] ?? styles.new
  const Icon = status.icon
  const reason = organisation.status_reason || organisation.notes
  const fill = [
    { label: 'Title', value: organisation.fill_rates.title },
    { label: words.location, value: organisation.fill_rates.offices },
    { label: words.group, value: organisation.fill_rates.groups },
    { label: 'Education', value: organisation.fill_rates.education },
  ]
  return (
    <Table.Tr>
      <Table.Td>
        <Group gap={6} wrap="nowrap">
          <Anchor component={Link} to={`/people?organisation=${organisation.id}`} size="sm" fw={600}>
            {organisation.name}
          </Anchor>
          <Tooltip label="Open their website">
            <ActionIcon component="a" href={organisation.website} target="_blank" rel="noreferrer" variant="subtle" size="sm" color="gray" aria-label={`${organisation.name} website`}>
              <IconExternalLink size={14} />
            </ActionIcon>
          </Tooltip>
          {onEdit && (
            <Tooltip label="Edit settings">
              <ActionIcon variant="subtle" size="sm" onClick={onEdit} aria-label={`Edit ${organisation.name}`}>
                <IconPencil size={14} />
              </ActionIcon>
            </Tooltip>
          )}
          {!organisation.enabled && (
            <Badge size="xs" color="gray">
              paused
            </Badge>
          )}
        </Group>
      </Table.Td>
      <Table.Td maw={320}>
        <Tooltip label={reason ? `${status.help} ${reason}` : status.help} multiline w={300}>
          <Badge variant="light" color={status.color} leftSection={<Icon size={12} />} style={{ flexShrink: 0 }}>
            {status.label}
          </Badge>
        </Tooltip>
        {showDetails && reason && (
          <Text size="xs" c="dimmed" mt={4} lineClamp={3}>
            {reason}
          </Text>
        )}
      </Table.Td>
      <Table.Td ta="right" fw={700}>
        {organisation.in_scope.toLocaleString()}
      </Table.Td>
      <Table.Td ta="right">{organisation.departed}</Table.Td>
      <Table.Td>
        <Freshness value={organisation.last_discovery_at} />
      </Table.Td>
      {showDetails && (
        <>
          <Table.Td ta="right">
            <Text size="sm">
              {organisation.discovered.toLocaleString()} / {organisation.fetched.toLocaleString()} / {organisation.queued_jobs.toLocaleString()}
            </Text>
            {organisation.extraction_failures > 0 && (
              <Text size="xs" c="orange">
                {organisation.extraction_failures} couldn't be read
              </Text>
            )}
          </Table.Td>
          <Table.Td miw={200}>
            <Stack gap={2}>
              {fill.map((f) => (
                <Group key={f.label} gap={6} wrap="nowrap">
                  <Text size="xs" w={70} truncate>
                    {f.label}
                  </Text>
                  <Progress value={(f.value ?? 0) * 100} size="sm" w={80} color={(f.value ?? 0) < 0.5 ? 'orange' : 'teal'} aria-label={`${f.label} ${pct(f.value)}`} />
                  <Text size="xs" c="dimmed">
                    {pct(f.value)}
                  </Text>
                </Group>
              ))}
            </Stack>
          </Table.Td>
        </>
      )}
    </Table.Tr>
  )
}
