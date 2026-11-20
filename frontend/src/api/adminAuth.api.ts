import { apiFetch } from './client'

export interface RoleCatalogItem {
  code: string
  name: string
  privileged: boolean
}

export interface MunicipalUserItem {
  id: string
  email: string
  full_name: string
  is_active: boolean
  roles: string[]
  created_at: string
  updated_at: string
}

export interface AdminUsersListResponse {
  users: MunicipalUserItem[]
  available_roles: RoleCatalogItem[]
}

export interface UserRolesUpdateResponse {
  user: MunicipalUserItem
  message: string
}

export interface CurrentSessionMeResponse {
  user_id: string
  email: string
  full_name: string
  is_active: boolean
  roles: string[]
}

export async function getAdminUsers(): Promise<AdminUsersListResponse> {
  return apiFetch<AdminUsersListResponse>('/api/v1/admin/users', { auth: true })
}

export async function updateAdminUserRoles(
  userId: string,
  roles: string[],
): Promise<UserRolesUpdateResponse> {
  return apiFetch<UserRolesUpdateResponse>(`/api/v1/admin/users/${userId}/roles`, {
    method: 'PUT',
    auth: true,
    body: JSON.stringify({ roles }),
  })
}

export async function getCurrentSessionMe(): Promise<CurrentSessionMeResponse> {
  return apiFetch<CurrentSessionMeResponse>('/api/v1/auth/me', { auth: true })
}
