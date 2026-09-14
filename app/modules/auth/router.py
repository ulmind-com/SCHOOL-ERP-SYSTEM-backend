from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.core.config import settings
from app.core.deps import CurrentUser, OptionalTenantDep, TenantDep
from app.core.exceptions import Forbidden, Unauthorized
from app.core.navigation import build_navigation
from app.core.security import decode_token
from app.db.mongo import C, collection
from app.models.base import Msg
from app.modules.auth import service
from app.modules.auth.schemas import (
    AcceptInviteRequest,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    InstitutionChoice,
    LoginRequest,
    LoginResponse,
    MeResponse,
    RefreshRequest,
    ResetPasswordRequest,
    TokenPair,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=LoginResponse, summary="Sign in to an institution")
async def login(payload: LoginRequest, request: Request, tenant: OptionalTenantDep = None):
    """Signs a user into their institution.

    When the deployment is dedicated, or the host/header already names an
    institution, that one is used. Otherwise the email is looked up across
    institutions: one match signs straight in, several return 409 with the list
    so the client can ask which.
    """
    resolved = await service.resolve_login_tenant(payload.email, payload.institution, tenant)
    user, auth, tokens = await service.authenticate(
        payload.email, payload.password, resolved,
        request=request, remember=payload.remember_me,
    )
    return LoginResponse(
        tokens=tokens,
        user=service.session_user(user, auth),
        institution=await service.session_institution(resolved),
        must_change_password=bool(user.get("must_change_password")),
    )


@router.post("/platform/login", response_model=LoginResponse,
             summary="Sign in to the platform console (SaaS deployments only)")
async def platform_login(payload: LoginRequest, request: Request):
    user, auth, tokens = await service.authenticate_platform(
        payload.email, payload.password, request=request
    )
    return LoginResponse(tokens=tokens, user=service.session_user(user, auth))


@router.get("/institutions", response_model=list[InstitutionChoice],
            summary="Institutions an email can sign in to")
async def institutions_for_email(email: str):
    if settings.is_dedicated:
        return []
    return await service.find_institutions_for_email(email)


@router.post("/refresh", response_model=TokenPair, summary="Exchange a refresh token")
async def refresh(payload: RefreshRequest, request: Request):
    claims = decode_token(payload.refresh_token, "refresh")
    if claims is None:
        raise Unauthorized("Your session has expired. Sign in again.")
    return await service.rotate_refresh_token(payload.refresh_token, claims, request=request)


@router.post("/logout", response_model=Msg, summary="End the current session")
async def logout(payload: RefreshRequest):
    await service.revoke_session(payload.refresh_token)
    return Msg(detail="Signed out")


@router.post("/logout-all", response_model=Msg, summary="End every session for this account")
async def logout_all(auth: CurrentUser):
    count = await service.revoke_all_sessions(auth.user_id)
    return Msg(detail=f"Signed out of {count} session(s)")


@router.get("/me", response_model=MeResponse, summary="Current user, institution and navigation")
async def me(auth: CurrentUser, tenant: OptionalTenantDep = None):
    target = C.PLATFORM_USERS if auth.is_platform else C.USERS
    user = await collection(target).find_one({"_id": auth.user_id}) or {}
    return MeResponse(
        user=service.session_user(user, auth),
        institution=(
            await service.session_institution(tenant)
            if tenant is not None and not auth.is_platform else None
        ),
        navigation=build_navigation(auth, tenant),
        deployment_mode=settings.deployment_mode,
    )


@router.post("/change-password", response_model=Msg, summary="Change your own password")
async def change_password(payload: ChangePasswordRequest, auth: CurrentUser, request: Request):
    await service.change_password(
        auth, payload.current_password, payload.new_password, request=request
    )
    return Msg(detail="Password updated. Sign in again on your other devices.")


@router.post("/forgot-password", response_model=Msg, status_code=status.HTTP_202_ACCEPTED,
             summary="Request a password reset link")
async def forgot_password(payload: ForgotPasswordRequest, tenant: OptionalTenantDep = None):
    resolved = await service.resolve_login_tenant(payload.email, payload.institution, tenant)
    token = await service.start_password_reset(payload.email, resolved)
    if token:
        await service.email_password_reset(payload.email, token)
    response = Msg(detail="If that address has an account, a reset link is on its way.")
    if settings.debug and token:
        # Development convenience only — never returned once DEBUG is off.
        response.detail += f" [dev token: {token}]"
    return response


@router.post("/reset-password", response_model=Msg, summary="Set a new password from a reset link")
async def reset_password(payload: ResetPasswordRequest):
    await service.finish_password_reset(payload.token, payload.new_password)
    return Msg(detail="Password set. You can sign in now.")


@router.post("/accept-invite", response_model=LoginResponse, summary="Activate an invited account")
async def accept_invite(payload: AcceptInviteRequest, request: Request, tenant: TenantDep):
    user = await service.accept_invite(payload.token, payload.password, payload.full_name)
    if user.get("tenant_id") != tenant.id:
        raise Forbidden("This invitation belongs to a different institution")
    auth = await service.build_auth_context(user, tenant.id)
    tokens = await service._issue_tokens(user["_id"], tenant_id=tenant.id, request=request)
    return LoginResponse(
        tokens=tokens,
        user=service.session_user(user, auth),
        institution=await service.session_institution(tenant),
    )
