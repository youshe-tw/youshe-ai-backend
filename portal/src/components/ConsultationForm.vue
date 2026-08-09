<template>
  <section class="consultation-widget">
    <h2>快速諮詢
      <span class="en-text">Quick Consultation</span>
    </h2>
    <form @submit.prevent="handleSubmit" class="consultation-form">
      <div class="form-group">
        <label for="name">姓名 Name</label>
        <input type="text" id="name" v-model="formData.name" required>
      </div>
      <div class="form-group">
        <label for="phone">電話 Phone</label>
        <input type="tel" id="phone" v-model="formData.phone" required>
      </div>
      <div class="form-group">
        <label for="email">電子郵件 Email</label>
        <input type="email" id="email" v-model="formData.email" required>
      </div>
      <div class="form-group">
        <label for="service">需求服務 Service</label>
        <select id="service" v-model="formData.service" required>
          <option value="">請選擇服務項目</option>
          <option value="住宅設計">住宅設計</option>
          <option value="商業空間設計">商業空間設計</option>
          <option value="裝修諮詢">裝修諮詢</option>
        </select>
      </div>
      <div class="form-group">
        <label for="message">留言 Message</label>
        <textarea id="message" v-model="formData.message" rows="4"></textarea>
      </div>
      <button type="submit" class="submit-btn" :disabled="sending">
        {{ sending ? '發送中...' : '送出諮詢' }}
        <span v-if="!sending" class="en-text">Submit</span>
      </button>
    </form>
  </section>
</template>

<script>
export default {
  name: 'ConsultationForm',
  data() {
    return {
      formData: {
        name: '',
        phone: '',
        email: '',
        service: '',
        message: ''
      },
      sending: false
    }
  },
  methods: {
    async handleSubmit() {
      this.sending = true;
      
      try {
        // 這裡可以添加其他表單處理邏輯
        console.log('Form submitted:', this.formData);
        alert('感謝您的諮詢！我們會盡快與您聯繫。\nThank you for your inquiry! We will contact you soon.');
        this.resetForm();
      } catch (error) {
        console.error('Form submission failed:', error);
        alert('發送失敗，請稍後再試。\nSubmission failed, please try again later.');
      } finally {
        this.sending = false;
      }
    },
    resetForm() {
      this.formData = {
        name: '',
        phone: '',
        email: '',
        service: '',
        message: ''
      };
    }
  }
}
</script>

<style scoped>
/* 可以保留原有的 CSS 樣式 */
</style> 