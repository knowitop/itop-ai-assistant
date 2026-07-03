import { AppShell, Badge, Group, NavLink, Title } from '@mantine/core';
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

  useEffect(() => {
    fetchHealth().then(setHealth).catch(() => setHealth(null));
    fetchSetupStatus().then(setSetup).catch(() => setSetup(null));
  }, []);

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
        <Outlet />
      </AppShell.Main>
    </AppShell>
  );
}
