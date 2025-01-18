// Add text
export function addText(text: string): void {
    const input = document.getElementById("denial_text") as HTMLTextAreaElement;
    input.value += text;
}

// Error messages
function rehideHiddenMessage(name: string): void {
    document.getElementById(name).classList.remove('visible');
}
function showHiddenMessage(name: string): void {
    document.getElementById(name).classList.add('visible');
}
export function hideErrorMessages(event: Event): void {
    const form = document.getElementById("fuck_health_insurance_form") as HTMLFormElement;
    if (form == null) {
	return
    }
    if(form.privacy.checked && form.personalonly.checked && form.tos.checked) {
	rehideHiddenMessage('agree_chk_error');
    }
    if (form.pii.checked) {
	rehideHiddenMessage('pii_error');
    }
    if (form.email.value.length > 1) {
	document.getElementById('email-label').style.color="";
	rehideHiddenMessage('email_error');
    }
    if (form.denial_text.value.length > 1) {
	document.getElementById('denial_text_label').style.color="";
	rehideHiddenMessage('need_denial');
    }
}
export function validateScrubForm(event: Event): void {
    const form = event.target as HTMLFormElement;
    if(!form.privacy.checked || !form.personalonly.checked || !form.tos.checked) {
	showHiddenMessage('agree_chk_error');
    } else {
	rehideHiddenMessage('agree_chk_error');
    }
    if (!form.pii.checked) {
	showHiddenMessage('pii_error');
    } else {
	rehideHiddenMessage('pii_error');
    }
    if (form.email.value.length < 1) {
	showHiddenMessage('email_error');
	document.getElementById('email-label').style.color="red";
    } else {
	document.getElementById('email-label').style.color="";
	rehideHiddenMessage('email_error');
    }
    if (form.denial_text.value.length < 1) {
	showHiddenMessage('need_denial');
	document.getElementById('denial_text_label').style.color="red";
    } else {
	document.getElementById('denial_text_label').style.color="";
	rehideHiddenMessage('need_denial');
    }

    if (form.pii.checked && form.privacy.checked && form.email.value.length > 0) {
	rehideHiddenMessage('agree_chk_error');
	rehideHiddenMessage('pii_error');
	rehideHiddenMessage('email_error');
	rehideHiddenMessage('need_denial');
	// YOLO
	return;
    } else {
	// Bad news no submit
	event.preventDefault();
    }
}
