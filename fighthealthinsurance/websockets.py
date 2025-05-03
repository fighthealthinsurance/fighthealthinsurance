import json
from loguru import logger
import asyncio

from channels.generic.websocket import AsyncWebsocketConsumer

from fighthealthinsurance import common_view_logic


class StreamingAppealsBackend(AsyncWebsocketConsumer):
    """Streaming back the appeals as json :D"""

    async def connect(self):
        logger.debug("Accepting connection for appeals")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug("Disconnecting appeals")
        pass

    async def receive(self, text_data):
        data = json.loads(text_data)
        logger.debug("Starting generation of appeals...")
        aitr = common_view_logic.AppealsBackendHelper.generate_appeals(data)
        # We do a try/except here to log since the WS framework swallow exceptions sometimes
        try:
            await asyncio.sleep(1)
            await self.send("\n")
            async for record in aitr:
                await asyncio.sleep(0)
                await self.send("\n")
                await asyncio.sleep(0)
                logger.debug(f"Sending record {record}")
                await self.send(record)
                await asyncio.sleep(0)
                await self.send("\n")
        except Exception as e:
            logger.opt(exception=True).debug(f"Error sending back appeals: {e}")
            raise e
        finally:
            await asyncio.sleep(1)
            await self.close()
        logger.debug("All sent")


class StreamingEntityBackend(AsyncWebsocketConsumer):
    """Streaming Entity Extraction"""

    async def connect(self):
        logger.debug("Accepting connection for entity extraction")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug("Disconnecting entity extraction")
        pass

    async def receive(self, text_data):
        data = json.loads(text_data)
        aitr = common_view_logic.DenialCreatorHelper.extract_entity(data["denial_id"])

        try:
            async for record in aitr:
                logger.debug(f"Sending record {record}")
                await self.send(record)
                await asyncio.sleep(0)
                await self.send("\n")
            await asyncio.sleep(1)
            logger.debug(f"Sent all records")
        except Exception as e:
            logger.opt(exception=True).debug(f"Error sending back entity: {e}")
            raise e
        finally:
            await asyncio.sleep(1)
            await self.close()
            logger.debug("Closed connection")


class PriorAuthConsumer(AsyncWebsocketConsumer):
    """Streaming back the proposed prior authorizations as JSON."""

    async def connect(self):
        logger.debug("Accepting connection for prior auth streaming")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug(f"Disconnecting prior auth streaming with code {close_code}")
        pass

    async def receive(self, text_data):
        data = json.loads(text_data)
        logger.debug("Starting generation of prior auth proposals...")

        # Validate the token and ID
        token = data.get("token")
        prior_auth_id = data.get("id")

        if not token or not prior_auth_id:
            await self.send(json.dumps({"error": "Missing token or prior auth ID"}))
            await self.close()
            return

        # Get the prior auth request
        try:
            prior_auth = await sync_to_async(self._get_prior_auth_request)(
                prior_auth_id, token
            )
            if not prior_auth:
                await self.send(json.dumps({"error": "Invalid token or prior auth ID"}))
                await self.close()
                return

            # Check if the prior auth has answers
            if not prior_auth.answers:
                await self.send(
                    json.dumps({"error": "Prior auth request does not have answers"})
                )
                await self.close()
                return

            # Generate prior auth proposals
            await self.send(
                json.dumps(
                    {
                        "status": "generating",
                        "message": "Starting to generate prior authorization proposals",
                    }
                )
            )

            # Update status
            await sync_to_async(self._update_prior_auth_status)(
                prior_auth, "prior_auth_requested"
            )

            # Generate proposals
            generator = self._generate_prior_auth_proposals(prior_auth)

            # We do a try/except here to log since the WS framework may swallow exceptions
            try:
                await asyncio.sleep(1)
                async for proposal in generator:
                    await asyncio.sleep(0)  # Allow other tasks to run
                    logger.debug(f"Sending proposal {proposal['proposed_id']}")
                    await self.send(json.dumps(proposal))
                    await asyncio.sleep(0)
            except Exception as e:
                logger.opt(exception=True).debug(
                    f"Error sending back prior auth proposals: {e}"
                )
                raise e
            finally:
                await asyncio.sleep(1)
                await self.close()

            logger.debug("All prior auth proposals sent")
        except Exception as e:
            logger.opt(exception=True).debug(f"Error in prior auth consumer: {e}")
            await self.send(json.dumps({"error": f"Server error: {str(e)}"}))
            await self.close()

    def _get_prior_auth_request(self, prior_auth_id, token):
        """Get the prior auth request and validate the token."""
        try:
            prior_auth = PriorAuthRequest.objects.get(id=prior_auth_id)
            if str(prior_auth.token) != str(token):
                return None
            return prior_auth
        except PriorAuthRequest.DoesNotExist:
            return None

    def _update_prior_auth_status(self, prior_auth, status):
        """Update the status of the prior auth request."""
        prior_auth.status = status
        prior_auth.save()
        return prior_auth

    async def _generate_prior_auth_proposals(self, prior_auth):
        """Generate prior auth proposals using ML models."""
        from fighthealthinsurance.ml.ml_router import ml_router

        # Extract relevant information
        diagnosis = prior_auth.diagnosis
        treatment = prior_auth.treatment
        insurance_company = prior_auth.insurance_company
        patient_health_history = prior_auth.patient_health_history
        questions = prior_auth.questions
        answers = prior_auth.answers

        # Prepare prompt context
        context = {
            "diagnosis": diagnosis,
            "treatment": treatment,
            "insurance_company": insurance_company,
            "patient_health_history": patient_health_history,
            "qa_pairs": [],
        }

        # Add Q&A pairs
        if questions and answers:
            for i, (question, _) in enumerate(questions):
                if i < len(answers) and answers.get(str(i)):
                    context["qa_pairs"].append(
                        {"question": question, "answer": answers.get(str(i), "")}
                    )

        # Get available models
        models = ml_router.generate_text_backends()

        # Generate 2-3 different proposals
        num_proposals = min(len(models), 3)

        for i in range(num_proposals):
            # Select different models for variety
            model = models[i % len(models)]

            try:
                # Generate the proposal text
                prompt = f"""
                Generate a prior authorization request letter for {treatment} to treat {diagnosis}.
                Insurance Company: {insurance_company}

                Use the following information from the patient's answers:
                """

                for qa in context["qa_pairs"]:
                    prompt += f"\nQ: {qa['question']}\nA: {qa['answer']}\n"

                if patient_health_history:
                    prompt += (
                        f"\nAdditional Patient History:\n{patient_health_history}\n"
                    )

                prompt += """
                Format the prior authorization request as a formal letter with:
                1. Date and header
                2. Patient and provider information (use placeholders)
                3. Clear statement of the requested treatment/procedure
                4. Medical necessity justification
                5. Supporting evidence and clinical rationale
                6. Relevant billing codes if available
                7. Closing with provider details

                Make it persuasive, evidence-based, and compliant with insurance requirements.
                """

                # Generate the text - wrap in try/except since different backends might have different interfaces
                try:
                    proposal_text = await model.generate_text(prompt)
                except:
                    # Fallback to a different method signature if needed
                    proposal_text = await model.generate_text(prompt=prompt)

                # Create and save the proposal
                proposed_id = uuid.uuid4()
                proposal = await sync_to_async(self._create_proposal)(
                    prior_auth, proposed_id, proposal_text
                )

                # Yield the proposal to be sent to the client
                yield {"proposed_id": str(proposed_id), "text": proposal_text}

                # Small delay between generations
                await asyncio.sleep(1)

            except Exception as e:
                logger.opt(exception=True).debug(f"Error generating proposal: {e}")
                # Continue with next model despite errors
                continue

    def _create_proposal(self, prior_auth, proposed_id, text):
        """Create a proposal in the database."""
        proposal = ProposedPriorAuth.objects.create(
            proposed_id=proposed_id, prior_auth_request=prior_auth, text=text
        )
        return proposal


class OngoingChatConsumer(AsyncWebsocketConsumer):
    """WebSocket consumer for ongoing chat with LLM for pro users."""

    async def connect(self):
        logger.debug("Accepting connection for ongoing chat")
        await self.accept()

    async def disconnect(self, close_code):
        logger.debug(f"Disconnecting ongoing chat with code {close_code}")
        pass

    async def receive(self, text_data):
        data = json.loads(text_data)
        logger.debug("Received message for ongoing chat")

        # Get the required data
        message = data.get("message")
        chat_id = data.get("chat_id")

        # Get the user from scope (authenticated by Django Channels)
        user = self.scope.get("user")

        if not user or not user.is_authenticated:
            await self.send(json.dumps({"error": "Authentication required"}))
            await self.close()
            return

        try:
            # Get or create professional user
            professional_user = await sync_to_async(self._get_professional_user)(user)

            if not professional_user:
                await self.send(json.dumps({"error": "Professional user not found"}))
                await self.close()
                return

            # Get or create the chat session
            chat = await sync_to_async(self._get_or_create_chat)(
                professional_user, chat_id
            )

            # Add the user message to the chat history
            await sync_to_async(self._add_message_to_history)(chat, "user", message)

            # Generate response
            response = await self._generate_llm_response(chat, message)

            # Send response to the client
            await self.send(json.dumps({"chat_id": str(chat.id), "response": response}))

        except Exception as e:
            logger.opt(exception=True).debug(f"Error in ongoing chat: {e}")
            await self.send(json.dumps({"error": f"Server error: {str(e)}"}))

        finally:
            await self.close()

    def _get_professional_user(self, user):
        """Get the professional user from the Django user."""
        try:
            return ProfessionalUser.objects.get(user=user, active=True)
        except ProfessionalUser.DoesNotExist:
            return None

    def _get_or_create_chat(self, professional_user, chat_id=None):
        """Get an existing chat or create a new one."""
        if chat_id:
            try:
                return OngoingChat.objects.get(
                    id=chat_id, professional_user=professional_user
                )
            except OngoingChat.DoesNotExist:
                # Fall through to create a new chat
                pass

        # Create a new chat
        return OngoingChat.objects.create(
            professional_user=professional_user,
            chat_history=[],
            summary_for_next_call={},
        )

    def _add_message_to_history(self, chat, role, content):
        """Add a message to the chat history."""
        if not chat.chat_history:
            chat.chat_history = []

        chat.chat_history.append(
            {"role": role, "content": content, "timestamp": timezone.now().isoformat()}
        )
        chat.save()
        return chat

    async def _generate_llm_response(self, chat, message):
        """Generate a response from the LLM."""
        from fighthealthinsurance.ml.ml_router import ml_router

        # Get the available text generation model
        models = ml_router.generate_text_backends()
        if not models:
            return "Sorry, no language models are currently available."

        model = models[0]  # Use the first available model

        # Prepare the system prompt
        system_prompt = """
        You are an AI assistant helping a healthcare professional with insurance and medical questions.
        Provide accurate, helpful, and concise information.
        Use professional language appropriate for healthcare settings.
        After your response, add the symbol 🐼 followed by CONTEXT: and then a brief summary of
        this conversation that will help you maintain context in future messages.
        The user will not see the content after the 🐼 symbol.
        """

        # Prepare the conversation history
        history = [{"role": "system", "content": system_prompt}]

        # Add context from previous summary if available
        if chat.summary_for_next_call:
            prev_context = chat.summary_for_next_call.get("summary", "")
            if prev_context:
                history.append(
                    {"role": "system", "content": f"Previous context: {prev_context}"}
                )

        # Add chat history (last few messages only)
        if chat.chat_history:
            # Only include the last 10 messages to avoid context overflow
            for msg in chat.chat_history[-10:]:
                history.append({"role": msg["role"], "content": msg["content"]})

        # Add the current message
        history.append({"role": "user", "content": message})

        try:
            # Generate response
            response = await model.generate_chat_response(history)

            # Extract the response text and context summary
            if "🐼" in response:
                response_text, context_part = response.split("🐼", 1)
                context_summary = context_part.replace("CONTEXT:", "").strip()

                # Save the context summary
                await sync_to_async(self._update_context_summary)(chat, context_summary)

                # Add the assistant's response to the chat history
                await sync_to_async(self._add_message_to_history)(
                    chat, "assistant", response_text.strip()
                )

                return response_text.strip()
            else:
                # If no separator, use the whole response
                await sync_to_async(self._add_message_to_history)(
                    chat, "assistant", response
                )
                return response

        except Exception as e:
            logger.opt(exception=True).debug(f"Error generating LLM response: {e}")
            return "Sorry, I encountered an error while processing your request."

    def _update_context_summary(self, chat, summary):
        """Update the context summary for the chat."""
        chat.summary_for_next_call = {"summary": summary}
        chat.save()
        return chat
