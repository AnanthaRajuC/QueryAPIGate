import '@testing-library/jest-dom/vitest';
import { cleanup, configure } from '@testing-library/react';
import { afterEach } from 'vitest';

import { resetSession } from '@/auth/session';

// The API Repository is a lazily loaded chunk with several requests behind it; give findBy* room.
configure({ asyncUtilTimeout: 5000 });

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  resetSession(); // each test's fake backend answers /auth/session afresh
});
