import { Alert, Anchor, AppShell, Badge, Group, NavLink, Title } from '@mantine/core';
import { useEffect, useState } from 'react';
import { Link, Outlet, useLocation } from 'react-router-dom';

import { fetchHealth, fetchSetupStatus, Health, SetupStatus } from './api';

const NAV = [
  { to: '/setup', label: 'Setup' },
  { to: '/connections', label: 'Connections' },
  { to: '/modules', label: 'Modules' },
  { to: '/prompts', label: 'Prompts' },
  { to: '/runs', label: 'Runs' },
];

export default function Layout() {
  const location = useLocation();
  const [health, setHealth] = useState<Health | null>(null);
  const [setup, setSetup] = useState<SetupStatus | null>(null);

  // Refetched on every navigation so the badges reflect wizard progress.
  useEffect(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null));
    fetchSetupStatus().then(setSetup).catch(() => setSetup(null));
  }, [location.pathname]);

  return (
    <AppShell header={{ height: 56 }} navbar={{ width: 220, breakpoint: 'sm' }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Title order={4}>iTop AI Assistant</Title>
          <Group gap="xs">
            {health ? (
              <Badge color={health.redis ? 'green' : 'yellow'} variant="light">
                {health.redis ? 'redis ok' : 'redis degraded'}
              </Badge>
            ) : (
              <Badge color="red" variant="light">
                offline
              </Badge>
            )}
            {setup && (
              <Badge color={setup.configured ? 'green' : 'orange'} variant="light">
                {setup.configured ? 'configured' : 'setup required'}
              </Badge>
            )}
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Navbar p="xs">
        {NAV.map((item) => (
          <NavLink
            key={item.to}
            component={Link}
            to={item.to}
            label={item.label}
            active={location.pathname.startsWith(item.to)}
          />
        ))}
      </AppShell.Navbar>
      <AppShell.Main>
        {setup && !setup.configured && location.pathname !== '/setup' && (
          <Alert color="orange" mb="md" title="Setup required">
            The assistant is not configured yet — /webhook returns 503 until the LLM and iTop
            connections are set.{' '}
            <Anchor component={Link} to="/setup">
              Run the setup wizard
            </Anchor>
            .
          </Alert>
        )}
        <Outlet />
      </AppShell.Main>
    </AppShell>
  );
}
