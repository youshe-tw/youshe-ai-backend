document.addEventListener('DOMContentLoaded', function() {
    // 初始化 EmailJS
    emailjs.init("YOUR_PUBLIC_KEY"); // 替換為您的 EmailJS public key

    const form = document.getElementById('consultationForm');
    
    form.addEventListener('submit', function(e) {
        e.preventDefault();
        
        // 顯示發送中訊息
        const submitBtn = form.querySelector('.submit-btn');
        const originalText = submitBtn.innerHTML;
        submitBtn.innerHTML = '發送中...';
        submitBtn.disabled = true;

        // 收集表單數據
        const formData = new FormData(form);
        const templateParams = {
            name: formData.get('name'),
            phone: formData.get('phone'),
            email: formData.get('email'),
            service: formData.get('service'),
            message: formData.get('message')
        };

        // 發送郵件
        emailjs.send('YOUR_SERVICE_ID', 'YOUR_TEMPLATE_ID', templateParams)
            .then(function(response) {
                alert('感謝您的諮詢！我們會盡快與您聯繫。\nThank you for your inquiry! We will contact you soon.');
                form.reset();
            }, function(error) {
                alert('發送失敗，請稍後再試。\nSending failed, please try again later.');
                console.error('EmailJS error:', error);
            })
            .finally(function() {
                submitBtn.innerHTML = originalText;
                submitBtn.disabled = false;
            });
    });
}); 