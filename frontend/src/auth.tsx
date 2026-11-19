import { Alert, Button, Center, Paper, PasswordInput, Stack, TextInput, Title } from '@mantine/core'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { api, unwrap } from './api/client'

export function LoginPage() {
  const queryClient = useQueryClient()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const words = useQuery({ queryKey: ['vocabulary'], queryFn: () => unwrap(api.GET('/api/vocabulary')) })
  const login = useMutation({
    mutationFn: () => unwrap(api.POST('/api/auth/login', { body: { email, password } })),
    onSuccess: (me) => queryClient.setQueryData(['me'], me),
  })

  return (
    <Center mih="100vh" p="md">
      <Paper withBorder p="xl" w={380} maw="100%">
        <form
          onSubmit={(event) => {
            event.preventDefault()
            login.mutate()
          }}
        >
          <Stack>
            <Title order={3}>{words.data?.app_name ?? 'Radar'}</Title>
            {login.error && <Alert color="red">{login.error.message}</Alert>}
            <TextInput label="Email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.currentTarget.value)} />
            <PasswordInput label="Password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.currentTarget.value)} />
            <Button type="submit" loading={login.isPending}>
              Sign in
            </Button>
          </Stack>
        </form>
      </Paper>
    </Center>
  )
}
