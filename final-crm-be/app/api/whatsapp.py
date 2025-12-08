"""
WhatsApp API Endpoints
Provides HTTP endpoint for WhatsApp integration
"""
import asyncio

from fastapi import APIRouter, HTTPException, Depends, status, Response
from fastapi.responses import StreamingResponse
import logging
import io

from app.models.whatsapp import (
    SessionActivateRequest,
    SessionActivateResponse,
    SessionStatusResponse,
    SessionTerminateResponse,
    QRCodeResponse
)
from app.services.whatsapp_service import get_whatsapp_service, WhatsAppService
from app.auth.dependencies import get_current_user
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


# ============================================
# WHATSAPP SESSION ACTIVATION ENDPOINT
# ============================================

@router.post(
    "/activate",
    response_model=SessionActivateResponse,
    status_code=status.HTTP_200_OK,
    summary="Activate WhatsApp session",
    description="Register a new WhatsApp session and generate QR code for authentication"
)
async def activate_whatsapp_session(
    request: SessionActivateRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Activate WhatsApp session for an agent.

    This endpoint performs the following:
    1. Registers a new WhatsApp session using agent_id as session_id
    2. Generates QR code for the session
    3. Returns session details and QR code for scanning

    The agent can then scan the QR code with their WhatsApp mobile app
    to authenticate and activate the session.

    Args:
        request: Session activation request with agent_id
        current_user: Current authenticated user

    Returns:
        SessionActivateResponse with session details and QR code

    Raises:
        HTTPException: If session activation fails
    """
    try:
        whatsapp_service = get_whatsapp_service()

        # Use agent_id as session_id
        session_id = request.agent_id

        logger.info(f"🔄 Activating WhatsApp session for agent: {session_id}")

        # Step 0: terminate activating session
        terminate_whatsapp_session = await whatsapp_service.terminate_session(session_id)
        print("terminate_whatsapp_session : "+str(terminate_whatsapp_session))

        # Step 1: Register the session
        registration_result = await whatsapp_service.register_session(session_id)

        if not registration_result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to register WhatsApp session"
            )

        logger.info(f"✅ WhatsApp session registered: {session_id}")

        # step 1.5: wait for a moment to allow QR code generation
        await asyncio.sleep(1)

        # Step 2: Get QR code for authentication
        try:
            qr_result = await whatsapp_service.get_qr_code(session_id, as_image=False)

            print("QR Result : ")
            print(qr_result)

            if qr_result.get("success"):
                qr_code = qr_result.get("qr_code")
                logger.info(f"✅ QR code generated for session: {session_id}")
            else:
                qr_code = None
                logger.warning(f"⚠️  QR code not available yet for session: {session_id}")

        except Exception as qr_error:
            # QR code might not be ready immediately
            qr_code = None
            logger.warning(f"⚠️  QR code generation pending for session {session_id}: {qr_error}")

        # Prepare response
        response = SessionActivateResponse(
            success=True,
            session_id=session_id,
            status=registration_result.get("status", "pending"),
            qr_code=qr_code,
            qr_image_url=f"/whatsapp/qr/{session_id}/image" if qr_code else None,
            message="WhatsApp session activated successfully. Please scan the QR code to authenticate.",
            data={
                "registration": registration_result,
                "instructions": "Scan the QR code with your WhatsApp mobile app to authenticate this session."
            }
        )

        logger.info(f"✅ WhatsApp session activation completed for agent {session_id} by user {current_user.user_id}")

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error activating WhatsApp session for agent {request.agent_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to activate WhatsApp session: {str(e)}"
        )


# ============================================
# HELPER ENDPOINT: GET QR CODE AS STRING
# ============================================

@router.get(
    "/qr/{session_id}",
    response_model=QRCodeResponse,
    summary="Get QR code string",
    description="Get QR code as text string for frontend QR generation"
)
async def get_qr_code_string(
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get QR code as text string.

    This endpoint returns the QR code data as a string that can be used
    to generate QR code images in the frontend using libraries like qrcode.js,
    qrcode.react, or similar.

    **Use Cases:**
    - Generate custom-styled QR codes in frontend
    - Store QR code data for later regeneration
    - More flexibility than direct image display

    **Frontend Integration Example (React):**
    ```javascript
    import QRCode from 'qrcode.react';

    function QRDisplay({ qrString }) {
      return (
        <QRCode
          value={qrString}
          size={300}
          level="H"
          includeMargin={true}
        />
      );
    }
    ```

    Args:
        session_id: Session identifier
        current_user: Current authenticated user

    Returns:
        QRCodeResponse with QR code string data

    Raises:
        HTTPException: If QR code retrieval fails

    Example Response:
        {
            "success": true,
            "session_id": "agent-uuid",
            "format": "text",
            "qr_code": "2@xxx...base64_qr_data...xxx",
            "data": {...}
        }
    """
    try:
        whatsapp_service = get_whatsapp_service()

        logger.info(f"📝 Retrieving QR code string for session: {session_id}")

        # Get QR code as text string
        qr_result = await whatsapp_service.get_qr_code(session_id, as_image=False)

        if not qr_result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="QR code not available for this session"
            )

        logger.info(f"✅ QR code string retrieved for session: {session_id}")

        return QRCodeResponse(**qr_result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving QR code string for session {session_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve QR code string: {str(e)}"
        )


# ============================================
# HELPER ENDPOINT: GET QR CODE IMAGE
# ============================================

@router.get(
    "/qr/{session_id}/image",
    summary="Get QR code image",
    description="Get QR code as PNG image for session authentication"
)
async def get_qr_code_image(
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Get QR code as PNG image.

    This is a helper endpoint to retrieve the QR code image
    that can be displayed in the frontend.

    Args:
        session_id: Session identifier
        current_user: Current authenticated user

    Returns:
        PNG image of the QR code

    Raises:
        HTTPException: If QR code retrieval fails
    """
    try:
        whatsapp_service = get_whatsapp_service()

        logger.info(f"📷 Retrieving QR code image for session: {session_id}")

        # Get QR code as image
        qr_result = await whatsapp_service.get_qr_code(session_id, as_image=True)

        if not qr_result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="QR code not available for this session"
            )

        # Return image as PNG
        image_bytes = qr_result.get("data")

        return StreamingResponse(
            io.BytesIO(image_bytes),
            media_type="image/png",
            headers={
                "Content-Disposition": f"inline; filename=qr_{session_id}.png"
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error retrieving QR code image for session {session_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to retrieve QR code image: {str(e)}"
        )


# ============================================
# SESSION STATUS CHECK ENDPOINT
# ============================================

@router.get(
    "/status/{session_id}",
    response_model=SessionStatusResponse,
    summary="Check WhatsApp session status",
    description="Check the connection status of a WhatsApp session and retrieve phone number if authenticated"
)
async def check_session_status(
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Check WhatsApp session status.

    This endpoint checks whether a WhatsApp session is:
    - authenticated: Session is active and connected
    - pending: Session started but not authenticated (QR code not scanned)
    - not_found: Session does not exist
    - error: Error checking session status

    If the session is authenticated (status=200 and success=true), this endpoint
    will also fetch the phone number from /client/getClassInfo/{session_id}

    Use this endpoint to:
    - Monitor session connection status
    - Check if QR code has been scanned
    - Verify session is ready before sending messages
    - Get the WhatsApp phone number associated with the session

    Args:
        session_id: Session identifier (usually agent_id)
        current_user: Current authenticated user

    Returns:
        SessionStatusResponse with status details and phone_number (if authenticated)

    Example Response:
        {
            "success": true,
            "session_id": "agent-uuid",
            "status": "authenticated",
            "connected": true,
            "message": "Session is active and authenticated",
            "phone_number": "+628123456789"
        }
    """
    try:
        whatsapp_service = get_whatsapp_service()

        logger.info(f"🔍 Checking WhatsApp session status: {session_id}")

        # Check session status
        status_result = await whatsapp_service.check_session_status(session_id)

        # log
        logger.info(f"status_result: {status_result}")

        # If session is authenticated and successful, get phone number from getClassInfo
        phone_number = None
        if status_result.get("success") and status_result.get("status") == "authenticated":
            try:
                logger.info(f"📞 Fetching phone number for authenticated session: {session_id}")
                class_info_result = await whatsapp_service.get_client_class_info(session_id)

                if class_info_result.get("success"):
                    # Extract phone number from sessionInfo.me.user
                    class_info_data = class_info_result.get("data", {})
                    session_info = class_info_data.get("sessionInfo", {})
                    me = session_info.get("me", {})
                    phone_number = me.get("user")

                    if phone_number:
                        logger.info(f"✅ Phone number retrieved: {phone_number}")
                    else:
                        logger.warning(f"⚠️  Phone number not found in class info for session {session_id}")
                else:
                    logger.warning(f"⚠️  Failed to get class info for session {session_id}: {class_info_result.get('message')}")

            except Exception as phone_error:
                logger.warning(f"⚠️  Failed to retrieve phone number for session {session_id}: {phone_error}")

        # Add phone_number to status result
        status_result["phone_number"] = phone_number

        logger.info(f"✅ Session status retrieved: {session_id} - {status_result.get('status')}")

        return SessionStatusResponse(**status_result)

    except Exception as e:
        logger.error(f"Error checking session status for {session_id}: {e}")
        # Return error status instead of raising exception
        return SessionStatusResponse(
            success=False,
            session_id=session_id,
            status="error",
            connected=False,
            message=f"Failed to check session status: {str(e)}",
            phone_number=None
        )


# ============================================
# SESSION TERMINATION ENDPOINT
# ============================================

@router.delete(
    "/terminate/{session_id}",
    response_model=SessionTerminateResponse,
    summary="Terminate WhatsApp session",
    description="Terminate and disconnect a WhatsApp session"
)
async def terminate_whatsapp_session(
    session_id: str,
    current_user: User = Depends(get_current_user)
):
    """
    Terminate WhatsApp session.

    This endpoint will:
    - Disconnect the WhatsApp session
    - Log out from WhatsApp Web
    - Remove session data from WhatsApp API service

    Use this endpoint when:
    - Agent wants to disconnect WhatsApp integration
    - Need to reset session for re-authentication
    - Agent is being deactivated or removed

    **Warning:** After termination, you need to call `/whatsapp/activate`
    again to create a new session and scan QR code.

    Args:
        session_id: Session identifier (usually agent_id)
        current_user: Current authenticated user

    Returns:
        SessionTerminateResponse with termination confirmation

    Example Response:
        {
            "success": true,
            "session_id": "agent-uuid",
            "message": "Session terminated successfully",
            "data": {...}
        }
    """
    try:
        whatsapp_service = get_whatsapp_service()

        logger.info(f"🛑 Terminating WhatsApp session: {session_id}")

        # Terminate the session
        result = await whatsapp_service.terminate_session(session_id)

        # delete data from agents_integrations table using supabase client where agent_id and channel is whatsapp
        supabase = get_whatsapp_service().get_supabase_client()
        response = supabase.table("agent_integrations").delete().eq("agent_id", session_id).eq("channel", "whatsapp").execute()
        if response.data:
            logger.info(f"✅ Deleted WhatsApp integration for agent {session_id} from database.")
        else:
            logger.warning(f"⚠️ No WhatsApp integration found for agent {session_id} in database to delete.")



        if not result.get("success"):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to terminate WhatsApp session"
            )

        logger.info(f"✅ WhatsApp session terminated: {session_id} by user {current_user.user_id}")

        return SessionTerminateResponse(**result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error terminating session {session_id}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to terminate session: {str(e)}"
        )
