import { expect, test } from '@playwright/test';

test('a new user can register through the web UI', async ({ page }) => {
  await page.goto('/');
  await page.getByPlaceholder('Email').fill(`e2e-${Date.now()}@example.test`);
  await page.getByPlaceholder('Password').fill('a-long-test-password');
  await page.getByRole('button', { name: 'Register' }).click();
  await expect(page.getByText('Signed in as')).toBeVisible();
});
