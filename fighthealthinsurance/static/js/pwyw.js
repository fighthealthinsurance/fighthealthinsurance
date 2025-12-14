(function(){
  function initPWYWForms(){
    document.querySelectorAll('.pwyw-form').forEach(form => {
      const submitBtn = form.querySelector('[data-pwyw-submit]');
      const thanks = form.querySelector('.pwyw-thanks');
      const customRadio = form.querySelector('input[type=radio][value=custom]');
      const customInput = form.querySelector('.pwyw-custom-input');

      function currentAmount(){
        const checked = form.querySelector('input[type=radio][name=pwyw_amount]:checked');
        if(!checked) return 0;
        if(checked.value === 'custom'){
          const v = parseInt(customInput.value,10);
          return isNaN(v) ? 0 : v;
        }
        return parseInt(checked.value,10);
      }

      customInput && customInput.addEventListener('input', () => {
        if(customRadio) customRadio.checked = true;
      });

      submitBtn.addEventListener('click', async () => {
        const amt = currentAmount();
        if(amt <= 0){
          // Free usage path
          if(thanks){
            thanks.hidden = false;
            thanks.textContent = 'Thanks! Totally fine to use this free.';
          }
          submitBtn.blur();
          return;
        }

        // Create Stripe checkout session via backend
        try {
          submitBtn.disabled = true;
          submitBtn.textContent = 'Processing...';

          const response = await fetch('/v0/pwyw/checkout', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
            },
            body: JSON.stringify({ amount: amt })
          });

          const data = await response.json();

          if(data.success && data.url){
            // Open Stripe checkout in a new window so user stays on current page
            window.open(data.url, '_blank');
            if(thanks){
              thanks.hidden = false;
              thanks.textContent = 'Thanks! Complete your donation in the new tab, then close it to continue here.';
            }
            submitBtn.disabled = false;
            submitBtn.textContent = 'Support (optional)';
          } else if(data.success && data.message){
            if(thanks){
              thanks.hidden = false;
              thanks.textContent = data.message;
            }
            submitBtn.disabled = false;
            submitBtn.textContent = 'Support (optional)';
          } else {
            throw new Error(data.error || 'Unknown error');
          }
        } catch(e){
          console.error('PWYW checkout error', e);
          alert('Sorry, there was an error processing your donation. Please try again.');
          submitBtn.disabled = false;
          submitBtn.textContent = 'Support (optional)';
        }
      });
    });

    // Fax PWYW integration if present
    const faxGroup = document.querySelector('.fax-pwyw-group');
    if(faxGroup){
      const form = faxGroup.closest('form');
      const customRadio = faxGroup.querySelector('input[type=radio][value=custom]');
      const customInput = faxGroup.querySelector('.fax-custom-input');
      const hiddenField = form ? form.querySelector('input[name=fax_amount]') || (function(){
        const hf = document.createElement('input');
        hf.type = 'hidden';
        hf.name = 'fax_amount';
        form.appendChild(hf);
        return hf;
      })() : null;

      function updateAmount(){
        const checked = faxGroup.querySelector('input[type=radio]:checked');
        let value = 0;
        if(checked){
          if(checked.value === 'custom'){
            const v = parseInt(customInput.value,10);
            value = isNaN(v) ? 0 : v;
          } else {
            value = parseInt(checked.value,10);
          }
        }
        if(hiddenField) hiddenField.value = value;
      }

      faxGroup.addEventListener('change', updateAmount);
      customInput && customInput.addEventListener('input', () => {
        if(customRadio) customRadio.checked = true;
        updateAmount();
      });
      updateAmount();
    }
  }

  if(document.readyState !== 'loading'){
    initPWYWForms();
  } else {
    document.addEventListener('DOMContentLoaded', initPWYWForms);
  }
})();
