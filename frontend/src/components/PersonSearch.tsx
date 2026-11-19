// The search box in the header: type part of a name, pick a result, land on that person's page.
// It searches everyone we have found (segment "all"), not only the default segment, so nobody is
// "missing" just because they sit outside the group shown on the People page.
import { Autocomplete, Group, Loader, Text } from '@mantine/core'
import { useDebouncedValue } from '@mantine/hooks'
import { IconSearch } from '@tabler/icons-react'
import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { useNavigate } from 'react-router'
import { api, unwrap } from '../api/client'
import { useWords } from '../lib'

export function PersonSearch() {
  const navigate = useNavigate()
  const words = useWords()
  const [text, setText] = useState('')
  const [search] = useDebouncedValue(text.trim(), 250)
  const results = useQuery({
    queryKey: ['person-search', search],
    queryFn: () => unwrap(api.GET('/api/people', { params: { query: { q: search, segment: 'all', status: 'active', page_size: 8 } } })),
    enabled: search.length >= 2,
  })
  const people = results.data?.items ?? []
  // Autocomplete options are strings, so each option's value is the person id; we look the rest up.
  const byId = new Map(people.map((p) => [p.id, p]))

  return (
    <Autocomplete
      aria-label={`Search ${words.people.toLowerCase()} by name`}
      placeholder={`Find a ${words.person.toLowerCase()}…`}
      leftSection={<IconSearch size={16} />}
      rightSection={results.isFetching ? <Loader size={14} /> : null}
      value={text}
      onChange={setText}
      data={people.map((p) => p.id)}
      filter={({ options }) => options} // the server already filtered by name
      renderOption={({ option }) => {
        const person = byId.get(option.value)
        return (
          <Group gap={6} wrap="nowrap">
            <Text size="sm" fw={600}>
              {person?.name ?? 'Unknown'}
            </Text>
            <Text size="xs" c="dimmed" truncate>
              {person?.title ? `${person.title} · ` : ''}
              {person?.org_name}
            </Text>
          </Group>
        )
      }}
      onOptionSubmit={(id) => {
        setText('')
        navigate(`/people/${id}`)
      }}
      comboboxProps={{ width: 380, position: 'bottom-start' }}
      w={{ base: 160, xs: 240, md: 300 }}
    />
  )
}
